"""
Solaryien Connect — Pro onboarding (steps 2-5) + admin verification review.

  Step 2  business profile   update_business_profile()
  Step 3  documents          store_document() (COI, license) + sign_agreement()
                             -> submit_for_verification() flags 'pending_verification'
  Step 5  confirmation       signup_confirmation_email()
  Admin                      list_pending() / approve() / reject()

Documents are stored as BLOBs in the DB (never under the web root) and are only
retrievable through the admin-token-protected endpoint.
"""
import logging

import lead_distribution as ld  # normalize_trade

log = logging.getLogger("solaryien.onboarding")

# Agreements a pro must complete before verification review.
REQUIRED_AGREEMENTS = (
    "background_check_authorization",
    "contractor_verification_agreement",
    "terms_of_service",
)


# ── Step 2: business profile ─────────────────────────────────────────────
def update_business_profile(conn, pro_id, *, trades=None, regions=None,
                            years_in_business=None, website=None, bio=None):
    conn.execute(
        "UPDATE pros SET years_in_business = ?, website = ?, bio = ? WHERE id = ?",
        (years_in_business, website, bio, pro_id))
    if trades is not None:
        conn.execute("DELETE FROM pro_trades WHERE pro_id = ?", (pro_id,))
        for t in trades:
            conn.execute("INSERT OR IGNORE INTO pro_trades (pro_id, trade) VALUES (?, ?)",
                         (pro_id, ld.normalize_trade(t)))
    if regions is not None:
        conn.execute("DELETE FROM pro_regions WHERE pro_id = ?", (pro_id,))
        for r in regions:
            conn.execute("INSERT OR IGNORE INTO pro_regions (pro_id, region_code) VALUES (?, ?)",
                         (pro_id, r))
    conn.commit()


# ── Step 3: documents + agreements ───────────────────────────────────────
def store_document(conn, pro_id, doc_type, filename, mime, content):
    """Store/replace a document BLOB (doc_type: 'coi' | 'license')."""
    conn.execute(
        """INSERT INTO pro_documents (pro_id, doc_type, filename, mime, content)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(pro_id, doc_type) DO UPDATE SET
               filename = excluded.filename, mime = excluded.mime,
               content = excluded.content, uploaded_at = CURRENT_TIMESTAMP""",
        (pro_id, doc_type, filename, mime, content))
    conn.commit()


def get_document(conn, pro_id, doc_type):
    return conn.execute(
        "SELECT filename, mime, content FROM pro_documents WHERE pro_id = ? AND doc_type = ?",
        (pro_id, doc_type)).fetchone()


def sign_agreement(conn, pro_id, agreement_type, signed_name, ip=None):
    conn.execute(
        """INSERT INTO pro_agreements (pro_id, agreement_type, signed_name, ip)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(pro_id, agreement_type) DO UPDATE SET
               signed_name = excluded.signed_name, signed_at = CURRENT_TIMESTAMP,
               ip = excluded.ip""",
        (pro_id, agreement_type, signed_name, ip))
    conn.commit()


def verification_checklist(conn, pro_id):
    docs = {r["doc_type"] for r in conn.execute(
        "SELECT doc_type FROM pro_documents WHERE pro_id = ?", (pro_id,)).fetchall()}
    agrees = {r["agreement_type"] for r in conn.execute(
        "SELECT agreement_type FROM pro_agreements WHERE pro_id = ?", (pro_id,)).fetchall()}
    return {
        "coi": "coi" in docs,
        "license": "license" in docs,
        "agreements": {a: (a in agrees) for a in REQUIRED_AGREEMENTS},
    }


def is_ready_for_verification(conn, pro_id):
    c = verification_checklist(conn, pro_id)
    return c["coi"] and all(c["agreements"].values())


def submit_for_verification(conn, pro_id):
    """If COI + all required agreements are in, flag the account Pending Verification."""
    if not is_ready_for_verification(conn, pro_id):
        return "incomplete"
    conn.execute(
        "UPDATE pros SET verification_status = 'pending_verification' WHERE id = ?",
        (pro_id,))
    conn.commit()
    log.info("Pro %s submitted for verification (pending)", pro_id)
    return "pending_verification"


# ── Admin review ─────────────────────────────────────────────────────────
def list_pending(conn):
    rows = conn.execute(
        """SELECT id, name, company, email, phone, years_in_business, website,
                  verification_status, created_at
           FROM pros WHERE verification_status = 'pending_verification'
           ORDER BY created_at""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["trades"] = [x["trade"] for x in conn.execute(
            "SELECT trade FROM pro_trades WHERE pro_id = ?", (r["id"],)).fetchall()]
        d["regions"] = [x["region_code"] for x in conn.execute(
            "SELECT region_code FROM pro_regions WHERE pro_id = ?", (r["id"],)).fetchall()]
        d["documents"] = [x["doc_type"] for x in conn.execute(
            "SELECT doc_type FROM pro_documents WHERE pro_id = ?", (r["id"],)).fetchall()]
        d["agreements"] = [dict(x) for x in conn.execute(
            "SELECT agreement_type, signed_name, signed_at FROM pro_agreements WHERE pro_id = ?",
            (r["id"],)).fetchall()]
        out.append(d)
    return out


def approve(conn, pro_id):
    conn.execute(
        "UPDATE pros SET verification_status = 'approved', status = 'active', "
        "in_good_standing = 1, rejection_reason = NULL WHERE id = ?", (pro_id,))
    conn.commit()
    log.info("Pro %s APPROVED -> active/eligible", pro_id)


def reject(conn, pro_id, reason=None):
    conn.execute(
        "UPDATE pros SET verification_status = 'rejected', status = 'pending', "
        "in_good_standing = 0, rejection_reason = ? WHERE id = ?", (reason, pro_id))
    conn.commit()
    log.info("Pro %s REJECTED (%s)", pro_id, reason)


# ── Step 5: confirmation email ───────────────────────────────────────────
def signup_confirmation_email(pro, plan=None, lp_claim=None):
    plan_name = {"standard": "Connect Standard", "pro": "Connect Pro",
                 "complete": "Connect Complete"}.get((plan or "").lower(), "your selected plan")
    lp_line = ""
    if lp_claim:
        lp_line = ("\nAs a Launch Partner, you've locked in 50% off Apex for 3 months and a "
                   "free 3-month Connect Standard plan (3 regions).\n")
    return ("Your Solaryien Connect application is in — pending verification",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"Thanks for signing up for Solaryien Connect. Here's where things stand:\n\n"
            f"  - Plan: {plan_name}\n"
            f"  - Status: Pending Verification\n"
            f"{lp_line}\n"
            f"Our team at Solaryien, Inc. is reviewing your documents (insurance, license "
            f"where required, background-check authorization, and signed agreements). You can "
            f"expect approval within 1-2 business days, and we'll email you as soon as your "
            f"account is verified and active.\n\n— Solaryien Connect")
