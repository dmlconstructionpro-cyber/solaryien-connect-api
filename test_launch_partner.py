"""
Launch Partner tests — counter, claims, conversion dates, reminders.
Run:  python test_launch_partner.py   (stdlib only)
"""
from datetime import datetime, timedelta

import database
import accounts
import launch_partner as lp


def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    assert cond, label


def make_pro(conn, i):
    return accounts.create_pro_account(
        conn, name=f"Pro {i}", company=f"Co {i}", email=f"p{i}@x.com", phone="x",
        password="pw", trades=["Flooring"], regions=["R5"])


def main():
    conn = database.init_db(database.connect(":memory:"))
    base = datetime(2026, 6, 1, 9, 0, 0)

    print("\n[1] Counter starts at 5000")
    s = lp.get_status(conn)
    ok("remaining == 5000", s["remaining"] == 5000)
    ok("total == 5000", s["total"] == 5000)
    ok("offer active", s["active"] is True)
    ok("status never exposes how many signed up (only remaining/total)",
       set(s.keys()) >= {"remaining", "total", "active"} and "claimed" not in s)

    print("\n[2] Each completed signup decrements by 1")
    p1 = make_pro(conn, 1)
    claim = lp.claim_seat(conn, p1, apex_tier="professional", now=base)
    ok("claim returned", claim is not None)
    ok("remaining == 4999", lp.get_status(conn)["remaining"] == 4999)
    p2 = make_pro(conn, 2)
    lp.claim_seat(conn, p2, now=base)
    ok("remaining == 4998 after second signup", lp.get_status(conn)["remaining"] == 4998)

    print("\n[3] Apex pricing is 50% off; trial is 3 months")
    ok("starter 150/300", lp.APEX_TIERS["starter"] == {"normal": 300, "launch": 150})
    ok("professional 300/600", lp.APEX_TIERS["professional"] == {"normal": 600, "launch": 300})
    ok("enterprise 600/1200", lp.APEX_TIERS["enterprise"] == {"normal": 1200, "launch": 600})
    ok("trial_end is +3 months", claim["trial_end"][:10] == "2026-09-01")

    print("\n[4] A pro can't double-claim (no double decrement)")
    again = lp.claim_seat(conn, p1, now=base)
    ok("second claim for same pro returns None", again is None)
    ok("remaining unchanged at 4998", lp.get_status(conn)["remaining"] == 4998)

    print("\n[5] Offer ends at zero -> normal pricing")
    conn.execute("UPDATE launch_partner SET remaining = 1 WHERE id = 1")
    conn.commit()
    p3 = make_pro(conn, 3)
    ok("last seat claims OK", lp.claim_seat(conn, p3, now=base) is not None)
    ok("remaining == 0", lp.get_status(conn)["remaining"] == 0)
    ok("offer now inactive", lp.get_status(conn)["active"] is False)
    p4 = make_pro(conn, 4)
    ok("signup after sellout gets no seat", lp.claim_seat(conn, p4, now=base) is None)
    ok("remaining stays 0 (never negative)", lp.get_status(conn)["remaining"] == 0)

    print("\n[6] Conversion reminders fire 14 and 7 days before trial end")
    # p1 trial ends 2026-09-01. 14 days before = 2026-08-18; 7 days = 2026-08-25.
    due14 = lp.reminders_due(conn, now="2026-08-19 09:00:00")
    ok("14-day reminder due for p1", any(d[0] == p1 and d[1] == 14 for d in due14))
    sent = lp.process_reminders(conn, now="2026-08-19 09:00:00")
    ok("a reminder was sent", sent >= 1)
    due14b = lp.reminders_due(conn, now="2026-08-19 09:00:00")
    ok("14-day reminder not re-sent", not any(d[0] == p1 and d[1] == 14 for d in due14b))
    due7 = lp.reminders_due(conn, now="2026-08-26 09:00:00")
    ok("7-day reminder due for p1", any(d[0] == p1 and d[1] == 7 for d in due7))

    print("\n[7] Cancelling stops reminders")
    lp.cancel(conn, p2)
    due = lp.reminders_due(conn, now="2026-08-26 09:00:00")
    ok("cancelled pro gets no reminders", not any(d[0] == p2 for d in due))

    print("\nALL LAUNCH PARTNER CHECKS PASSED")


if __name__ == "__main__":
    main()
