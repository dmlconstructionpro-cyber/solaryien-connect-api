"""
Solaryien Connect — Launch Partner program (beta).

A pro who onboards during the pre-launch period claims one of 5,000 seats and
joins as a free beta tester at their chosen tier (Starter / Professional /
Enterprise). The chosen tier is LOCKED for the whole free period — it can't be
upgraded until the free months are complete (this stops someone onboarding on a
small tier free then jumping up mid-program to grab seats/regions for free).

Program (identical across all tiers):
  Pre-launch  : pay nothing. 1 month of Solaryien Apex FREE at the chosen tier
                (full seat count) as a beta tester. A $50 background check is NOT
                charged at sign-up but must be completed before launch.
  At launch   : 3 months of Solaryien Connect FREE at the chosen tier, plus
                3 months of Solaryien Apex at 50% off the chosen tier.
  After 3 mo  : full pricing at the chosen tier.

The seat counter is a single DB row (launch_partner). Each completed signup
decrements it by 1; claims are recorded in launch_partner_claims. When all seats
are gone the offer ends automatically.
"""
import calendar
import logging
from datetime import datetime

log = logging.getLogger("solaryien.launch_partner")

# Free Apex beta access at sign-up, then the post-launch discount window length.
BETA_MONTHS = 1            # 1 free month of Apex at sign-up (beta tester)
TRIAL_MONTHS = 3           # at launch: 3 months free Connect + 3 months 50% Apex

# Solaryien Apex — annual list price per tier, and the 50%-off Launch Partner
# price applied for 3 months at launch.
APEX_TIERS = {
    "starter":      {"normal": 1200, "launch": 600,  "seats": 1},
    "professional": {"normal": 2500, "launch": 1250, "seats": 3},
    "enterprise":   {"normal": 4000, "launch": 2000, "seats": 5},
}
CONNECT_OFFER = ("1 month of Apex free now as a beta tester, then at launch "
                 "3 months of Connect free + 3 months of Apex at 50% off — at "
                 "your chosen tier. Tier is locked for the free period.")


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
        "beta_months": BETA_MONTHS,     # free Apex months at sign-up
        "trial_months": TRIAL_MONTHS,   # free Connect + 50%-off Apex months at launch
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
    """
    Set the chosen Apex tier. The tier is LOCKED once chosen for the duration of
    the free Launch Partner period — it can only be set while still empty, never
    changed (prevents free mid-program upgrades). Returns the effective tier.
    """
    row = conn.execute("SELECT apex_tier FROM launch_partner_claims WHERE pro_id = ?",
                       (pro_id,)).fetchone()
    if row and row["apex_tier"]:
        # Already locked in — ignore change requests, keep the original choice.
        if (apex_tier or "") != (row["apex_tier"] or ""):
            log.info("Pro %s tier change blocked (locked at %s)", pro_id, row["apex_tier"])
        return row["apex_tier"]
    conn.execute("UPDATE launch_partner_claims SET apex_tier = ? WHERE pro_id = ?",
                 (apex_tier, pro_id))
    conn.commit()
    return apex_tier


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
        seats = price["seats"]
        beta_line = (f"  - Now (beta): Solaryien Apex {tier.title()} FREE for 1 month — "
                     f"full {seats}-seat access to test and give feedback.\n")
        launch_line = (f"  - At launch: 3 months of Connect free + 3 months of Apex "
                       f"{tier.title()} at 50% off (${price['launch']}, normally "
                       f"${price['normal']}/yr).\n")
    else:
        beta_line = "  - Now (beta): Solaryien Apex FREE for 1 month at your chosen tier.\n"
        launch_line = ("  - At launch: 3 months of Connect free + 3 months of Apex at "
                       "50% off your chosen tier.\n")
    return ("Welcome, Launch Partner — you're in the beta",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"You're officially a Solaryien Connect Launch Partner — onboarded free.\n\n"
            f"{beta_line}{launch_line}"
            f"  - Your tier is locked in for the entire free period.\n\n"
            f"One thing to complete before launch: a one-time $50 background check. It is "
            f"NOT charged at sign-up — you'll get a separate notice with a window to complete "
            f"it. No contractor is active at launch without it.\n\n"
            f"As a beta tester your usage, feedback, and bug reports directly shape the "
            f"platform before we go live. Cancel anytime before full pricing begins from your "
            f"dashboard.\n\n— Solaryien Connect")


def reminder_email(pro, which, claim):
    return (f"Your Launch Partner free period ends in {which} days",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"Your Solaryien Connect Launch Partner free period (3 months of Connect free + "
            f"3 months of Apex at 50% off) ends on {claim['trial_end'][:10]} — "
            f"{which} days from now. After that, full annual pricing begins at your chosen "
            f"tier.\n\n"
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
