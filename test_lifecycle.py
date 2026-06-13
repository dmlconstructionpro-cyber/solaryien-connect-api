"""
Account lifecycle + accept/decline/redistribution tests.

Run:  python test_lifecycle.py   (stdlib only)
"""

from datetime import datetime

import database
import accounts
import lead_distribution as ld


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    assert cond, label


def main():
    conn = database.init_db(database.connect(":memory:"))
    base = datetime(2026, 6, 1, 9, 0, 0)

    print("\n[1] Signup creates a PENDING pro that is NOT yet eligible")
    pid = accounts.create_pro_account(
        conn, name="Test Pro", company="Test Co", email="t@x.com", phone="x",
        password="hunter2", trades=["Flooring (Tile, Hardwood, LVP)"], regions=["R5"])
    row = accounts.get_pro(conn, pid)
    ok("status is pending", row["status"] == "pending")
    ok("not in good standing", row["in_good_standing"] == 0)
    elig = ld.eligible_pros(conn, trade_category="Flooring", region_code="R5",
                            lead_id="x", now=base)
    ok("pending pro excluded from eligibility", pid not in [r["pro_id"] for r in elig])

    print("\n[2] Login works only with the correct password")
    ok("correct password authenticates", accounts.authenticate(conn, "t@x.com", "hunter2") is not None)
    ok("wrong password rejected", accounts.authenticate(conn, "t@x.com", "nope") is None)
    ok("password is hashed, not stored plaintext", row["password_hash"] != "hunter2")

    print("\n[3] After verify + activate, the pro becomes eligible")
    accounts.approve_and_activate(conn, pid, plan="pro", coverage_type="bundled")
    row = accounts.get_pro(conn, pid)
    ok("status active", row["status"] == "active")
    ok("in good standing", row["in_good_standing"] == 1)
    elig = ld.eligible_pros(conn, trade_category="Flooring", region_code="R5",
                            lead_id="x", now=base)
    ok("active pro now eligible", pid in [r["pro_id"] for r in elig])

    print("\n[4] Build a pool and distribute a lead (cap at 3 so spares remain)")
    pool = [pid]
    for i in range(2, 7):
        p = accounts.create_pro_account(
            conn, name=f"P{i}", company=f"C{i}", email=f"p{i}@x.com", phone="x",
            password="pw", trades=["Flooring"], regions=["R5"])
        accounts.approve_and_activate(conn, p)
        pool.append(p)
    # 6 eligible pros, send to exactly 3 -> 3 spares left for redistribution
    ld.create_lead(conn, "lead-A", "Flooring", "R5",
                   project_title="Bath", city="Orlando", now=base)
    recv = ld.distribute_lead(conn, "lead-A", now=base, max_pros=3)
    ok("exactly 3 pros received the lead", len(recv) == 3)

    print("\n[5] Accept updates status")
    acc = recv[0]
    ok("accept returns True", ld.accept_lead(conn, "lead-A", acc, now=base))
    log = {r["pro_id"]: r["status"] for r in ld.get_distribution_log(conn, "lead-A")}
    ok("accepted status recorded", log[acc] == "accepted")

    print("\n[6] Decline triggers fair redistribution to a new pro")
    decliner = recv[1]
    before = set(r["pro_id"] for r in ld.get_distribution_log(conn, "lead-A"))
    result = ld.decline_lead(conn, "lead-A", decliner, reason="Outside my service area", now=base)
    after = set(r["pro_id"] for r in ld.get_distribution_log(conn, "lead-A"))
    log = {r["pro_id"]: r["status"] for r in ld.get_distribution_log(conn, "lead-A")}
    ok("decline recorded", result["declined"] and log[decliner] == "declined")
    new_pros = after - before
    ok("a new pro was added on decline", len(new_pros) >= 1)
    ok("decliner was NOT re-sent the lead", decliner not in result["redistributed_to"])
    ok("active recipients restored to >= 3", ld.active_recipient_count(conn, "lead-A") >= 3)

    print("\n[7] Suspended pro stops being eligible")
    accounts.set_status(conn, pool[-1], "suspended")
    elig_ids = [r["pro_id"] for r in ld.eligible_pros(
        conn, trade_category="Flooring", region_code="R5", lead_id="new", now=base)]
    ok("suspended pro excluded", pool[-1] not in elig_ids)

    print("\nALL LIFECYCLE CHECKS PASSED")


if __name__ == "__main__":
    main()
