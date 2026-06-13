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

# Documents every contractor must upload at sign-up (none skippable).
REQUIRED_DOCS = ("gov_id", "insurance", "business_license")
# Workers' comp: exactly one of these must be provided.
WC_DOCS = ("wc_certificate", "wc_exclusion")
# Contractor license is CONDITIONAL — only required when the trade/state mandates
# it. The client enforces this based on the chosen trade; it is not part of the
# always-required readiness set below.


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
    """Store/replace a document BLOB. doc_type is one of: gov_id, insurance,
    business_license, contractor_license, wc_certificate, wc_exclusion (legacy:
    coi, license)."""
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
        "gov_id": "gov_id" in docs,
        "insurance": ("insurance" in docs) or ("coi" in docs),  # legacy alias
        "business_license": "business_license" in docs,
        "contractor_license": ("contractor_license" in docs) or ("license" in docs),
        "workers_comp": any(d in docs for d in WC_DOCS),
        "agreements": {a: (a in agrees) for a in REQUIRED_AGREEMENTS},
    }


def is_ready_for_verification(conn, pro_id):
    c = verification_checklist(conn, pro_id)
    return (c["gov_id"] and c["insurance"] and c["business_license"]
            and c["workers_comp"] and all(c["agreements"].values()))


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
    tier = (lp_claim or {}).get("apex_tier")
    tier_name = (tier or "your chosen").title() if tier else "your chosen"
    lp_line = ""
    if lp_claim:
        lp_line = (f"\nAs a Launch Partner you onboarded free at the {tier_name} tier "
                   f"(locked in): 1 month of Apex free now as a beta tester, then at launch "
                   f"3 months of Connect free + 3 months of Apex at 50% off.\n")
    return ("Your Solaryien Connect application is in — pending verification",
            f"Hi {pro.get('name', 'there')},\n\n"
            f"Thanks for joining Solaryien Connect. Here's where things stand:\n\n"
            f"  - Status: Pending Verification\n"
            f"  - Tier: {tier_name}\n"
            f"{lp_line}\n"
            f"Our team at Solaryien, Inc. is reviewing your documents (government ID, proof of "
            f"insurance, business license, contractor license where required, workers' comp, "
            f"and signed agreements). Expect approval within 1-2 business days.\n\n"
            f"IMPORTANT — background check: a one-time $50 background check is required before "
            f"the platform launches. It was NOT charged at sign-up. You'll get a separate "
            f"notice with a window to complete it; no contractor is active at launch without "
            f"it.\n\n— Solaryien Connect")
