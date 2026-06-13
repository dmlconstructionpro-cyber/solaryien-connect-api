"""
Self-contained test/demo for the Solaryien Connect lead distribution system.

Run:  python test_distribution.py
Uses only the standard library (sqlite3). Proves all six rules.
"""

import logging
from datetime import datetime, timedelta

import database
from lead_distribution import (
    submit_homeowner_project, distribute_lead, eligible_pros,
    get_distribution_log, get_pro_lead_counts,
)


def add_pro(conn, name, company, trades, regions, status="active", good=1):
    cur = conn.execute(
        "INSERT INTO pros (name, company, status, in_good_standing) VALUES (?,?,?,?)",
        (name, company, status, good),
    )
    pid = cur.lastrowid
    for t in trades:
        conn.execute("INSERT INTO pro_trades (pro_id, trade) VALUES (?,?)", (pid, t))
    for r in regions:
        conn.execute("INSERT INTO pro_regions (pro_id, region_code) VALUES (?,?)", (pid, r))
    conn.commit()
    return pid


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    assert cond, label


def main():
    # The under-fill scenarios below intentionally log warnings; quiet them
    # so the test output is just PASS/FAIL lines.
    logging.getLogger("solaryien.lead_distribution").setLevel(logging.ERROR)
    conn = database.init_db(database.connect(":memory:"))

    # ── Seed pros ────────────────────────────────────────────────────────
    # Six flooring pros in R5 (the eligible pool we expect to rotate through)
    flooring_r5 = [add_pro(conn, f"Pro {i}", f"Flooring Co {i}",
                           ["Flooring"], ["R5"]) for i in range(1, 7)]
    # Wrong trade (painting, R5) — must NEVER receive a flooring lead
    painter = add_pro(conn, "Painter", "Paint Co", ["Painting"], ["R5"])
    # Right trade, wrong region (R4) — must NEVER receive an R5 lead
    wrong_region = add_pro(conn, "Tampa Floors", "Flooring Co T", ["Flooring"], ["R4"])
    # Right trade + region but SUSPENDED — must NEVER receive
    suspended = add_pro(conn, "Suspended", "Flooring Co S", ["Flooring"], ["R5"],
                        status="suspended")
    # Right trade + region but NOT in good standing — must NEVER receive
    bad_standing = add_pro(conn, "BadStanding", "Flooring Co B", ["Flooring"], ["R5"],
                           good=0)

    base = datetime(2026, 6, 1, 9, 0, 0)

    print("\n[1] Eligibility — region, trade, status, good standing")
    recv = submit_homeowner_project(
        conn, "lead-0001", "Flooring (Tile, Hardwood, LVP)", "R5",
        project_title="Master bath tile", city="Orlando", now=base)
    ok("3–5 pros selected (got %d)" % len(recv), 3 <= len(recv) <= 5)
    ok("painter (wrong trade) excluded", painter not in recv)
    ok("wrong-region pro excluded", wrong_region not in recv)
    ok("suspended pro excluded", suspended not in recv)
    ok("bad-standing pro excluded", bad_standing not in recv)
    ok("all winners are eligible flooring/R5 pros", set(recv) <= set(flooring_r5))

    print("\n[2] No duplicate sends — re-distributing the same lead")
    again = distribute_lead(conn, "lead-0001", now=base)
    log = get_distribution_log(conn, "lead-0001")
    pro_ids = [r["pro_id"] for r in log]
    ok("no pro appears twice for lead-0001", len(pro_ids) == len(set(pro_ids)))

    print("\n[3] Logging — every distribution recorded with a timestamp")
    ok("distribution log non-empty", len(log) >= 3)
    ok("each row has a timestamp", all(r["distributed_at"] for r in log))

    print("\n[4] Fair rotation — 60 leads spread evenly across the 6 pros")
    t = base
    for n in range(2, 62):
        t = t + timedelta(minutes=30)
        submit_homeowner_project(
            conn, f"lead-{n:04d}", "Flooring", "R5",
            project_title=f"Job {n}", city="Orlando", now=t)

    counts = {r["id"]: r["total_leads"] for r in get_pro_lead_counts(conn)
              if r["id"] in flooring_r5}
    spread = max(counts.values()) - min(counts.values())
    print("    per-pro totals:", counts)
    print("    spread (max-min):", spread)
    # 61 leads * (3..5 each) across 6 pros; fair rotation keeps the gap tiny
    ok("rotation is fair (max-min spread <= 2)", spread <= 2)
    ok("every eligible pro received leads", all(v > 0 for v in counts.values()))

    print("\n[5] Ineligible pros still received nothing after 61 leads")
    final = {r["id"]: r["total_leads"] for r in get_pro_lead_counts(conn)}
    ok("painter total == 0", final.get(painter, 0) == 0)
    ok("wrong-region total == 0", final.get(wrong_region, 0) == 0)
    ok("suspended total == 0", final.get(suspended, 0) == 0)
    ok("bad-standing total == 0", final.get(bad_standing, 0) == 0)

    print("\n[6] Too few eligible pros — distribute to all available, no crash")
    add_pro(conn, "Lonely Roofer", "Roof Co", ["Roofing"], ["R1"])
    recv2 = submit_homeowner_project(conn, "lead-roof", "Roofing", "R1", now=base)
    ok("1 eligible pro -> 1 sent (graceful under-fill)", len(recv2) == 1)

    print("\n[7] No eligible pros — lead held, returns empty, no crash")
    recv3 = submit_homeowner_project(conn, "lead-none", "HVAC", "R7", now=base)
    ok("0 eligible pros -> empty result", recv3 == [])

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
