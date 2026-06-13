"""
Solaryien Connect — Lead Distribution System.

When a homeowner submits a project, select 3–5 verified pros to receive it.

Selection rules (exactly as specified)
--------------------------------------
1. Pro must be SUBSCRIBED to the region the project is in.       (pro_regions)
2. Pro must OFFER the trade/service the homeowner needs.         (pro_trades)
3. Pro must be ACTIVE and IN GOOD STANDING.                      (pros.status / in_good_standing)
4. FAIR ROTATION — prioritize pros who have received fewer
   *recent* leads, so distribution evens out over time.         (pro_lead_count + recent window)
5. NEVER send the same lead to the same pro twice.              (UNIQUE(lead_id,pro_id) + filter)
6. LOG every distribution: which pros received which lead, when. (lead_distributions + logging)

The pool is capped at MAX_PROS (5). At least MIN_PROS (3) are notified when
that many eligible pros exist; if fewer are eligible, all of them are notified
and the shortfall is logged (we never fabricate a pro or send to an ineligible one).
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger("solaryien.lead_distribution")

MIN_PROS = 3
MAX_PROS = 5
ROTATION_WINDOW_DAYS = 30   # "recent" window used for fair rotation


# ── Trade normalization ──────────────────────────────────────────────────
# Homeowner-facing select values are verbose ("Flooring (Tile, Hardwood, LVP)").
# Pros register against canonical categories. Normalize before matching.
_TRADE_ALIASES = {
    "flooring (tile, hardwood, lvp)": "Flooring",
    "painting (interior / exterior)": "Painting",
    "cabinets & millwork": "Cabinets & Millwork",
    "drywall & plastering": "Drywall & Plastering",
    "general contracting / remodeling": "General Contracting",
    "concrete & masonry": "Concrete & Masonry",
}


def normalize_trade(trade):
    """Map a homeowner trade string to the canonical category pros register on."""
    if not trade:
        return trade
    key = trade.strip().lower()
    if key in _TRADE_ALIASES:
        return _TRADE_ALIASES[key]
    return trade.strip()


def _to_datetime(now=None):
    """Coerce None / datetime / 'YYYY-MM-DD HH:MM:SS' string to a datetime."""
    if now is None:
        return datetime.utcnow()
    if isinstance(now, datetime):
        return now
    return datetime.strptime(str(now), "%Y-%m-%d %H:%M:%S")


def _now_iso(now=None):
    """Accept an injected timestamp (str/datetime) for deterministic testing."""
    return _to_datetime(now).strftime("%Y-%m-%d %H:%M:%S")


# ── Lead creation ────────────────────────────────────────────────────────
def create_lead(conn, lead_id, trade_category, region_code,
                lead_type="residential", project_title=None, city=None, now=None):
    """Insert a submitted project. trade_category is normalized for matching."""
    conn.execute(
        """INSERT INTO leads (lead_id, lead_type, project_title, trade_category,
                              region_code, city, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, lead_type, project_title, normalize_trade(trade_category),
         region_code, city, _now_iso(now)),
    )
    conn.commit()
    return lead_id


# ── Eligibility + fair-rotation ordering ─────────────────────────────────
def eligible_pros(conn, *, trade_category, region_code, lead_id, now=None):
    """
    Return eligible pro ids ordered best-first for fair rotation.

    Eligible = subscribed to region (rule 1) AND offers trade (rule 2)
               AND active + good standing (rule 3)
               AND has NOT already received this lead (rule 5).

    Order   = fewest recent leads first, then fewest lifetime leads,
              then longest since last lead (never-received pros first),
              then pro id for a stable tie-break.  (rule 4)
    """
    since = _now_iso(_to_datetime(now) - timedelta(days=ROTATION_WINDOW_DAYS))
    rows = conn.execute(
        """
        SELECT p.id AS pro_id,
               COALESCE(recent.cnt, 0)        AS recent_leads,
               COALESCE(plc.total_leads, 0)   AS total_leads,
               COALESCE(plc.last_lead_at, '') AS last_lead_at
        FROM pros p
        JOIN pro_regions r ON r.pro_id = p.id AND r.region_code = :region
        JOIN pro_trades  t ON t.pro_id = p.id AND t.trade       = :trade
        LEFT JOIN pro_lead_count plc ON plc.pro_id = p.id
        LEFT JOIN (
            SELECT pro_id, COUNT(*) AS cnt
            FROM lead_distributions
            WHERE distributed_at >= :since
            GROUP BY pro_id
        ) recent ON recent.pro_id = p.id
        WHERE p.status = 'active'
          AND p.in_good_standing = 1
          AND p.id NOT IN (
                SELECT pro_id FROM lead_distributions WHERE lead_id = :lead_id
          )
        ORDER BY recent_leads ASC,
                 total_leads  ASC,
                 last_lead_at  ASC,   -- '' (never received) sorts first
                 p.id          ASC
        """,
        {"region": region_code, "trade": normalize_trade(trade_category),
         "since": since, "lead_id": lead_id},
    ).fetchall()
    return rows


# ── Distribution ─────────────────────────────────────────────────────────
def distribute_lead(conn, lead_id, *, now=None,
                    min_pros=MIN_PROS, max_pros=MAX_PROS):
    """
    Distribute a single lead to 3–5 eligible pros using fair rotation.

    Returns the list of pro ids that received the lead. Idempotent-safe:
    a pro who already has this lead is never selected again (rule 5).
    """
    lead = conn.execute(
        "SELECT trade_category, region_code FROM leads WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    if lead is None:
        raise ValueError(f"Unknown lead_id: {lead_id!r}")

    ordered = eligible_pros(
        conn, trade_category=lead["trade_category"],
        region_code=lead["region_code"], lead_id=lead_id, now=now,
    )

    chosen = [row["pro_id"] for row in ordered[:max_pros]]

    if not chosen:
        log.warning("Lead %s: NO eligible pros (trade=%s region=%s). "
                    "Lead held for re-distribution.",
                    lead_id, lead["trade_category"], lead["region_code"])
    elif len(chosen) < min_pros:
        log.warning("Lead %s: only %d eligible pro(s) (< target %d). "
                    "Distributing to all available.",
                    lead_id, len(chosen), min_pros)

    ts = _now_iso(now)
    recorded = []
    for pro_id in chosen:
        cur = conn.execute(
            """INSERT OR IGNORE INTO lead_distributions
                   (lead_id, pro_id, distributed_at, status)
               VALUES (?, ?, ?, 'sent')""",
            (lead_id, pro_id, ts),
        )
        if cur.rowcount == 0:
            # already had it (defense-in-depth); don't double-count
            continue
        conn.execute(
            """INSERT INTO pro_lead_count (pro_id, total_leads, last_lead_at, updated_at)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(pro_id) DO UPDATE SET
                   total_leads  = total_leads + 1,
                   last_lead_at = excluded.last_lead_at,
                   updated_at   = excluded.updated_at""",
            (pro_id, ts, ts),
        )
        recorded.append(pro_id)
        log.info("Lead %s -> pro %s at %s", lead_id, pro_id, ts)

    conn.commit()
    log.info("Lead %s distributed to %d pro(s): %s", lead_id, len(recorded), recorded)
    return recorded


def submit_homeowner_project(conn, lead_id, trade_category, region_code,
                             lead_type="residential", project_title=None,
                             city=None, now=None):
    """Convenience: create the lead, then immediately distribute it."""
    create_lead(conn, lead_id, trade_category, region_code,
                lead_type=lead_type, project_title=project_title,
                city=city, now=now)
    return distribute_lead(conn, lead_id, now=now)


# ── Lead responses (accept / decline) ────────────────────────────────────
def _set_response(conn, lead_id, pro_id, status, reason, now):
    cur = conn.execute(
        """UPDATE lead_distributions
           SET status = ?, decline_reason = ?, responded_at = ?
           WHERE lead_id = ? AND pro_id = ? AND status = 'sent'""",
        (status, reason, _now_iso(now), lead_id, pro_id),
    )
    conn.commit()
    return cur.rowcount > 0


def accept_lead(conn, lead_id, pro_id, now=None):
    """A pro accepts a lead they received. Returns True if the state changed."""
    changed = _set_response(conn, lead_id, pro_id, "accepted", None, now)
    if changed:
        log.info("Lead %s ACCEPTED by pro %s", lead_id, pro_id)
    return changed


def active_recipient_count(conn, lead_id):
    """Pros currently holding the lead (sent or accepted — i.e. not declined)."""
    row = conn.execute(
        """SELECT COUNT(*) AS c FROM lead_distributions
           WHERE lead_id = ? AND status IN ('sent', 'accepted')""",
        (lead_id,),
    ).fetchone()
    return row["c"]


def decline_lead(conn, lead_id, pro_id, reason=None, now=None,
                 redistribute=True, min_pros=MIN_PROS, max_pros=MAX_PROS):
    """
    A pro declines a lead. Marks it declined and (by default) tops the active
    pool back up toward min_pros by sending to the next fairest eligible pros
    who have not already received this lead (rule 5 still holds).

    Returns {"declined": bool, "redistributed_to": [pro_id, ...]}.
    """
    declined = _set_response(conn, lead_id, pro_id, "declined", reason, now)
    if declined:
        log.info("Lead %s DECLINED by pro %s (reason=%s)", lead_id, pro_id, reason)

    added = []
    if declined and redistribute:
        need = min_pros - active_recipient_count(conn, lead_id)
        if need > 0:
            lead = conn.execute(
                "SELECT trade_category, region_code FROM leads WHERE lead_id = ?",
                (lead_id,)).fetchone()
            ordered = eligible_pros(
                conn, trade_category=lead["trade_category"],
                region_code=lead["region_code"], lead_id=lead_id, now=now)
            ts = _now_iso(now)
            for row in ordered[:need]:
                pid = row["pro_id"]
                cur = conn.execute(
                    """INSERT OR IGNORE INTO lead_distributions
                           (lead_id, pro_id, distributed_at, status)
                       VALUES (?, ?, ?, 'sent')""", (lead_id, pid, ts))
                if cur.rowcount == 0:
                    continue
                conn.execute(
                    """INSERT INTO pro_lead_count (pro_id, total_leads, last_lead_at, updated_at)
                       VALUES (?, 1, ?, ?)
                       ON CONFLICT(pro_id) DO UPDATE SET
                           total_leads = total_leads + 1,
                           last_lead_at = excluded.last_lead_at,
                           updated_at = excluded.updated_at""",
                    (pid, ts, ts))
                added.append(pid)
                log.info("Lead %s re-distributed to pro %s after a decline", lead_id, pid)
            conn.commit()

    return {"declined": declined, "redistributed_to": added}


# ── Read helpers (for the dashboard / audit log) ─────────────────────────
def get_pro_leads(conn, pro_id, statuses=("sent", "accepted", "declined")):
    """Leads a given pro has received, newest first — powers the Pro dashboard."""
    placeholders = ",".join("?" for _ in statuses)
    return conn.execute(
        f"""SELECT l.lead_id, l.project_title, l.trade_category, l.region_code,
                   l.city, l.lead_type, d.status, d.distributed_at, d.responded_at
            FROM lead_distributions d JOIN leads l ON l.lead_id = d.lead_id
            WHERE d.pro_id = ? AND d.status IN ({placeholders})
            ORDER BY d.distributed_at DESC""",
        (pro_id, *statuses),
    ).fetchall()



def get_distribution_log(conn, lead_id):
    """Audit trail: every pro that received a given lead, and when."""
    return conn.execute(
        """SELECT d.pro_id, p.company, d.distributed_at, d.status
           FROM lead_distributions d JOIN pros p ON p.id = d.pro_id
           WHERE d.lead_id = ? ORDER BY d.distributed_at""",
        (lead_id,),
    ).fetchall()


def get_pro_lead_counts(conn):
    """Running totals per pro (fairness report)."""
    return conn.execute(
        """SELECT p.id, p.company, COALESCE(c.total_leads, 0) AS total_leads,
                  c.last_lead_at
           FROM pros p LEFT JOIN pro_lead_count c ON c.pro_id = p.id
           ORDER BY total_leads DESC, p.id""",
    ).fetchall()
