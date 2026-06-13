"""
Onboarding + admin verification flow test.
Run:  python test_onboarding.py   (stdlib only)
"""
import database
import accounts
import onboarding as ob
import lead_distribution as ld


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    assert cond, label


def main():
    conn = database.init_db(database.connect(":memory:"))

    print("\n[1] Step 1 — create account (incomplete, not eligible)")
    pid = accounts.create_pro_account(
        conn, name="Casey Pro", company="Casey Co", email="casey@x.com",
        phone="239-555-0100", password="pw")
    pro = accounts.get_pro(conn, pid)
    ok("verification_status incomplete", pro["verification_status"] == "incomplete")
    ok("status pending", pro["status"] == "pending")
    ok("not eligible for leads", pid not in [r["pro_id"] for r in ld.eligible_pros(
        conn, trade_category="Flooring", region_code="R5", lead_id="x")])

    print("\n[2] Step 2 — business profile")
    ob.update_business_profile(conn, pid, trades=["Flooring (Tile, Hardwood, LVP)"],
                               regions=["R5", "R4", "R3"], years_in_business=8,
                               website="https://caseyco.com", bio="Tile experts.")
    pro = accounts.get_pro(conn, pid)
    ok("years saved", pro["years_in_business"] == 8)
    ok("website saved", pro["website"] == "https://caseyco.com")
    ok("trades normalized + saved", conn.execute(
        "SELECT trade FROM pro_trades WHERE pro_id=?", (pid,)).fetchone()["trade"] == "Flooring")
    ok("3 regions saved", conn.execute(
        "SELECT COUNT(*) c FROM pro_regions WHERE pro_id=?", (pid,)).fetchone()["c"] == 3)

    print("\n[3] Step 3 — documents + agreements gate verification")
    ok("not ready before docs", not ob.is_ready_for_verification(conn, pid))
    ob.store_document(conn, pid, "coi", "coi.pdf", "application/pdf", b"%PDF-1.4 fake coi")
    ok("still not ready (agreements missing)", not ob.is_ready_for_verification(conn, pid))
    ob.sign_agreement(conn, pid, "background_check_authorization", "Casey Pro")
    ob.sign_agreement(conn, pid, "contractor_verification_agreement", "Casey Pro")
    ob.sign_agreement(conn, pid, "terms_of_service", "Casey Pro")
    ok("ready after COI + 3 signatures", ob.is_ready_for_verification(conn, pid))
    status = ob.submit_for_verification(conn, pid)
    ok("status -> pending_verification", status == "pending_verification")

    print("\n[4] Document is stored and retrievable (admin)")
    doc = ob.get_document(conn, pid, "coi")
    ok("COI bytes stored", doc["content"] == b"%PDF-1.4 fake coi")
    ok("COI mime stored", doc["mime"] == "application/pdf")

    print("\n[5] Admin sees pending, with docs + signed agreements")
    pending = ob.list_pending(conn)
    ok("one pending pro", len(pending) == 1 and pending[0]["id"] == pid)
    ok("pending shows coi document", "coi" in pending[0]["documents"])
    ok("pending shows 3 signed agreements", len(pending[0]["agreements"]) == 3)

    print("\n[6] Approve -> active + eligible for leads")
    ob.approve(conn, pid)
    pro = accounts.get_pro(conn, pid)
    ok("verification approved", pro["verification_status"] == "approved")
    ok("status active", pro["status"] == "active")
    ok("now eligible for leads", pid in [r["pro_id"] for r in ld.eligible_pros(
        conn, trade_category="Flooring", region_code="R5", lead_id="x")])

    print("\n[7] Reject path")
    p2 = accounts.create_pro_account(conn, name="No Go", company="NG", email="ng@x.com",
                                     phone="x", password="pw")
    ob.reject(conn, p2, "Insurance expired")
    pro2 = accounts.get_pro(conn, p2)
    ok("rejected status", pro2["verification_status"] == "rejected")
    ok("rejection reason saved", pro2["rejection_reason"] == "Insurance expired")
    ok("rejected pro not eligible", pro2["status"] != "active")

    print("\nALL ONBOARDING CHECKS PASSED")


if __name__ == "__main__":
    main()
