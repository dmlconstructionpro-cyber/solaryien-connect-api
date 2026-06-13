"""
Solaryien Connect — Launch Partner offer.

A pro who signs up during the launch period claims one of 5,000 seats:
  - Solaryien Apex: 3-month trial at 50% off (Starter/Professional/Enterprise)
  - Solaryien Connect Standard (3 regions): free for 3 months, bundled

After 3 months Apex converts to normal annual/quarterly pricing and Connect
Standard converts to quarterly billing unless the pro chooses annual. They can
cancel anytime before the trial ends. Reminder emails go out 14 and 7 days
before conversion. When all 5,000 seats are gone the offer ends automatically.

The seat counter is a single DB row (launch_partner). Each completed signup
decrements it by 1; claims are recorded in launch_partner_claims.
"""
import calendar
import logging
from datetime import datetime

log = logging.getLogger("solaryien.launch_partner")

TRIAL_MONTHS = 3

# Solaryien Apex 3-month trial — 50% off.
APEX_TIERS = {
    "starter":      {"normal": 300,  "launch": 150},
    "professional": {"normal": 600,  "launch": 300},
    "enterprise":   {"normal": 1200, "launch": 600},
}
CONNECT_OFFER = ("Free 3-month Solaryien Connect Standard (3 regions), "
                 "bundled with any Apex trial.")


# ── time helpers (injectable now for deterministic tests) ────────────────
def _now(now=None):
    if isinstance(now, datetime):
        return now
    if now is None:
        return datetime.utcnow()
    return datetime.strptime(now, "%Y-%m-%d %H:%M:%S")


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def add_months(dt, n):
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


# ── status (for the counter / offer display) ─────────────────────────────
def get_status(conn):
    row = conn.execute(
        "SELECT remaining, total FROM launch_partner WHERE id = 1").fetchone()
    remaining = row["remaining"] if row else 0
    total = row["total"] if row else 0
    return {
        "active": remaining > 0,        # when False the offer disappears, normal pricing
        "remaining": remaining,         # show ONLY this (never how many signed up)
        "total": total,
        "trial_months": TRIAL_MONTHS,
        "apex_tiers": APEX_TIERS,
        "connect_offer": CONNECT_OFFER,
    }


# ── claim a seat (called when a pro completes signup) ────────────────────
def claim_seat(conn, pro_id, apex_tier=None, now=None):
    """
    Atomically take one seat and record the claim. Returns the claim dict, or
    None if the offer is sold out or this pro already claimed.
    """
    if conn.execute("SELECT 1 FROM launch_partner_claims WHERE pro_id = ?",
                    (pro_id,)).fetchone():
        return None  # already claimed — never double-decrement

    now_dt = _now(now)
    cur = conn.execute(
        "UPDATE launch_partner SET remaining = remaining - 1, updated_at = ? "
        "WHERE id = 1 AND remaining > 0", (_iso(now_dt),))
    if cur.rowcount == 0:
        conn.commit()
        return None  # sold out

    trial_end = add_months(now_dt, TRIAL_MONTHS)
    conn.execute(
        "INSERT INTO launch_partner_claims (pro_id, apex_tier, claimed_at, trial_end) "
        "VALUES (?, ?, ?, ?)", (pro_id, apex_tier, _iso(now_dt), _iso(trial_end)))
    conn.commit()
    remaining = conn.execute(
        "SELECT remaining FROM launch_partner WHERE id = 1").fetchone()["remaining"]
    log.info("Launch Partner seat claimed by pro %s (%d remaining)", pro_id, remaining)
    return {"pro_id": pro_id, "apex_tier": apex_tier, "claimed_at": _iso(now_dt),
            "trial_end": _iso(trial_end), "remaining": remaining}


def get_claim(conn, pro_id):
    r = conn.execute(
        "SELECT * FROM launch_partner_claims WHERE pro_id = ?", (pro_id,)).fetchone()
    return dict(r) if r else None


def set_apex_tier(conn, pro_id, apex_tier):
    conn.execute("UPDATE launch_partner_claims SET apex_tier = ? WHERE pro_id = ?",
                 (apex_tier, pro_id))
    conn.commit()


def cancel(conn, pro_id):
    conn.execute("UPDATE launch_partner_claims SET cancelled = 1 WHERE pro_id = ?",
                 (pro_id,))
    conn.commit()


# ── conversion reminders (run daily via a scheduled job) ─────────────────
def reminders_due(conn, now=None):
    """Return [(pro_id, which_days, claim_row), ...] for reminders to send."""
    now_dt = _now(now)
    due = []
    rows = conn.execute(
        "SELECT * FROM launch_partner_claims WHERE converted = 0 AND cancelled = 0"
    ).fetchall()
    for r in rows:
        end = datetime.strptime(r["trial_end"], "%Y-%m-%d %H:%M:%S")
        days_left = (end - now_dt).days
        if 7 < days_left <= 14 and not r["reminder_14_sent"]:
            due.append((r["pro_id"], 14, r))
        if 0 <= days_left <= 7 and not r["reminder_7_sent"]:
            due.append((r["pro_id"], 7, r))
    return due


def _mark_reminder(conn, pro_id, which):
    col = "reminder_14_sent" if which == 14 else "reminder_7_sent"
    conn.execute(f"UPDATE launch_partner_claims SET {col} = 1 WHERE pro_id = ?",
                 (pro_id,))
    conn.commit()


def confirmation_email(pro, claim):
    tier = (claim.get("apex_tier") or "").strip()
    if tier in APEX_TIERS:
        price = APEX_TIERS[tier]
        apex_line = (f"  - Solaryien Apex {tier.title()}: 3-month trial at "
                     f"${price['launch']} (normally ${price['normal']}) — 50% off.\n")
    else:
        apex_line = "  - Solaryien Apex: 3-month trial at 50% off.\n"
    return ("Welcome, Launch Partner — your offer is locked in",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"You're officially a Solaryien Connect Launch Partner.\n\n"
            f"{apex_line}"
            f"  - Solaryien Connect Standard (3 regions): free for 3 months.\n\n"
            f"Your trial runs through {claim['trial_end'][:10]}. After that, Apex converts "
            f"to normal annual or quarterly pricing and Connect Standard converts to quarterly "
            f"billing unless you choose annual. Cancel anytime before then from your dashboard "
            f"to avoid being charged.\n\n— Solaryien Connect")


def reminder_email(pro, which, claim):
    return (f"Your Launch Partner trial converts in {which} days",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"Your Solaryien Connect Launch Partner trial (50% off Apex + free Connect "
            f"Standard) converts to regular pricing on {claim['trial_end'][:10]} — "
            f"{which} days from now.\n\n"
            f"  - Apex converts to normal annual or quarterly pricing.\n"
            f"  - Connect Standard converts to quarterly billing unless you choose annual.\n\n"
            f"To avoid being charged, cancel before {claim['trial_end'][:10]} from your "
            f"dashboard.\n\n— Solaryien Connect")


def _default_mailer(to, subject, body):
    try:
        import emailer
        return emailer.send(to, subject, body)
    except Exception as e:  # emailer/config not importable in some test contexts
        log.info("EMAIL -> %s: %s (%s)", to, subject, e)
        return False


def process_reminders(conn, now=None, mailer=None):
    """Send any due 14/7-day reminders. mailer(to, subject, body); default sends email."""
    mailer = mailer or _default_mailer
    sent = 0
    for pro_id, which, claim in reminders_due(conn, now):
        pro = conn.execute("SELECT name, email FROM pros WHERE id = ?", (pro_id,)).fetchone()
        subj, body = reminder_email(dict(pro) if pro else {}, which, dict(claim))
        mailer(pro["email"] if pro else None, subj, body)
        _mark_reminder(conn, pro_id, which)
        sent += 1
    return sent


if __name__ == "__main__":
    import sys
    import config
    import database
    conn = database.init_db(database.connect(config.DB_PATH))
    if "--send-reminders" in sys.argv:
        print(f"Sent {process_reminders(conn)} reminder(s).")
    else:
        s = get_status(conn)
        print(f"Launch Partner: {s['remaining']}/{s['total']} seats remaining "
              f"(active={s['active']})")
