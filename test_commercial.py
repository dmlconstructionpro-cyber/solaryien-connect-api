"""
Commercial platform backend test.
Run:  python test_commercial.py   (stdlib only)
"""
import database
import accounts
import onboarding as ob
import project_owners as po
import commercial as cm


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    assert cond, label


def comm_pro(conn, i, trades, regs, coverage="bundled", work="commercial"):
    pid = accounts.create_pro_account(conn, name=f"Pro{i}", company=f"Co{i}",
                                      email=f"c{i}@x.com", phone="x", password="pw")
    accounts.approve_and_activate(conn, pid, plan="pro", coverage_type=coverage)
    conn.execute("UPDATE pros SET work_type=? WHERE id=?", (work, pid))
    conn.commit()
    ob.update_business_profile(conn, pid, trades=trades, regions=regs)
    return pid


def main():
    conn = database.init_db(database.connect(":memory:"))
    sent = []
    mail = lambda to, s, b: sent.append((to, s))

    print("\n[1] Project Owner account (free, no verification)")
    oid = po.create_owner(conn, first_name="Pat", last_name="Owner", company_name="Owner GC",
                          email="owner@x.com", phone="x", password="pw", title="PM")
    ok("owner created", oid is not None)
    ok("login ok", po.authenticate(conn, "owner@x.com", "pw") is not None)
    ok("bad login rejected", po.authenticate(conn, "owner@x.com", "nope") is None)

    print("\n[2] Commercial subscription gating")
    elec = comm_pro(conn, 1, ["Electrical"], ["R5"])                       # bundled, commercial
    floor = comm_pro(conn, 2, ["Flooring"], ["R5", "R4"], work="both")     # both
    res = comm_pro(conn, 3, ["Electrical"], ["R5"], work="residential")    # residential only
    noacc = comm_pro(conn, 4, ["Electrical"], ["R5"], coverage="residential")  # no commercial access
    farreg = comm_pro(conn, 5, ["Electrical"], ["R7"])                     # wrong region
    ok("bundled pro has commercial access",
       cm.has_commercial_access(conn, accounts.get_pro(conn, elec)))
    ok("residential-coverage pro denied",
       not cm.has_commercial_access(conn, accounts.get_pro(conn, noacc)))

    print("\n[3] Post project -> OPEN distribution to ALL matching commercial pros")
    res_post = cm.post_project(conn, oid, {
        "project_name": "Office Buildout", "project_address": "1 Main St",
        "project_city": "Orlando", "project_zip": "32801",
        "trades_needed": ["09", "16"], "project_description": "Tenant buildout.",
        "bid_due_date": "2026-08-01 17:00:00", "bond_required": 1}, mailer=mail)
    ok("region auto-assigned from ZIP (R5)", res_post["region_code"] == "R5")
    ok("notified ALL matching pros = 2 (elec + floor)", res_post["notified"] == 2)
    notified_ids = {r["pro_id"] for r in conn.execute("SELECT pro_id FROM commercial_notifications").fetchall()}
    ok("electrician + flooring notified", {elec, floor} <= notified_ids)
    ok("residential-only pro NOT notified", res not in notified_ids)
    ok("no-commercial-access pro NOT notified", noacc not in notified_ids)
    ok("wrong-region pro NOT notified", farreg not in notified_ids)
    uid = cm.get_project_row(conn, res_post["project_uid"])["project_uid"]

    print("\n[4] Pro sees project in their commercial feed")
    feed = cm.pro_commercial_projects(conn, elec)
    ok("electrician sees the project", any(p["project_uid"] == uid for p in feed))

    print("\n[5] Bids — submit, block resubmit, withdraw, resubmit")
    cm.submit_bid(conn, uid, elec, 125000, "Full electrical scope.")
    try:
        cm.submit_bid(conn, uid, elec, 130000, "again")
        dup = False
    except ValueError as e:
        dup = str(e) == "already_bid"
    ok("cannot resubmit while active", dup)
    pid_proj = cm.get_project_row(conn, uid)["id"]
    bid_id = cm.list_bids(conn, pid_proj)[0]["id"]
    ok("withdraw works", cm.withdraw_bid(conn, bid_id, elec))
    ok("can resubmit after withdraw", cm.submit_bid(conn, uid, elec, 128000, "Revised scope."))

    print("\n[6] Second bidder, then owner marks a winner (Part 10)")
    cm.submit_bid(conn, uid, floor, 90000, "Flooring scope.")
    bids = cm.list_bids(conn, pid_proj)
    ok("owner sees 2 active bids", len(bids) == 2)
    win_id = [b["id"] for b in bids if b["pro_id"] == elec][0]
    sent.clear()
    ok("mark win ok", cm.mark_win(conn, win_id, oid, mailer=mail))
    statuses = {b["pro_id"]: b["status"] for b in conn.execute(
        "SELECT pro_id, status FROM commercial_bids WHERE project_id=?", (pid_proj,)).fetchall()}
    ok("winner = won", statuses[elec] == "won")
    ok("other = lost", statuses[floor] == "lost")
    ok("project awarded", cm.get_project_row(conn, uid)["status"] == "awarded")
    ok("winner emailed", any("selected" in s.lower() for _, s in sent))

    print("\n[7] Invitations")
    ok("invite works", cm.invite_contractor(conn, uid, floor, oid, mailer=mail))
    inv = conn.execute("SELECT COUNT(*) c FROM project_invitations WHERE pro_id=?", (floor,)).fetchone()["c"]
    ok("invitation recorded", inv == 1)

    print("\nALL COMMERCIAL CHECKS PASSED")


if __name__ == "__main__":
    main()
