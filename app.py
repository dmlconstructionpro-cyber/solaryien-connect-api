"""
Solaryien Connect — Flask API (production-ready for Render).

Endpoints
  GET  /healthz                          health check (Render)
  POST /api/leads                        homeowner submits a project -> distribute
  GET  /api/leads/<id>/distributions     audit log for a lead
  POST /api/leads/<id>/accept            pro accepts a lead
  POST /api/leads/<id>/decline           pro declines -> fair redistribution
  POST /api/pro/signup                   create pending pro account
  POST /api/pro/login                    authenticate
  POST /api/pro/<id>/checkout-session    Stripe Checkout for a subscription
  POST /api/pro/<id>/subscribe           (dev) activate without Stripe
  GET  /api/pro/<id>/leads               pro dashboard leads
  GET  /api/pros/lead-counts             fairness report
  POST /api/stripe/webhook               Stripe -> activate pro on payment
  POST /api/apex/webhook                 Solaryien Apex -> ingest + distribute lead

Run locally:  python app.py
On Render:    gunicorn app:app  (see render.yaml / Procfile)
"""

import hashlib
import hmac
import json
import logging
import uuid

from flask import Flask, request, jsonify

import config
import database
import accounts
import auth
import commercial as cm
import csi
import emailer
import lead_distribution as ld
import launch_partner as lp
import onboarding
import project_owners as po
import regions
import stripe_integration

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("solaryien.api")

app = Flask(__name__)


def db():
    return database.init_db(database.connect(config.DB_PATH))


def _boot():
    """Initialise the schema (and optionally seed) once at startup."""
    conn = db()
    if config.SEED_ON_START:
        n = conn.execute("SELECT COUNT(*) c FROM pros").fetchone()["c"]
        if n == 0:
            import seed_pros
            seed_pros.seed(conn)
            log.info("Seeded demo pros on first boot.")
    conn.close()


_boot()


# ── CORS ─────────────────────────────────────────────────────────────────
@app.after_request
def cors(resp):
    allowed = config.allowed_origins_list()
    origin = request.headers.get("Origin")
    if allowed == "*":
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Stripe-Signature, X-Apex-Signature"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def preflight(_any):
    return ("", 204)


def _bearer():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else None


def _deny():
    return jsonify(error="authentication required"), 401


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/")
def root():
    return jsonify(service="solaryien-connect-api", status="ok")


# ── Homeowner: submit a project -> distribute ────────────────────────────
@app.post("/api/leads")
def submit_lead():
    data = request.get_json(force=True, silent=True) or {}
    trade = data.get("trade")
    # region may be explicit, else mapped from ZIP (approximate)
    region = data.get("region") or regions.region_for_zip(data.get("zip"))
    if not trade:
        return jsonify(error="trade is required"), 400
    if not region:
        return jsonify(error="could not determine a Florida region from the ZIP"), 400

    lead_id = data.get("lead_id") or f"lead_{uuid.uuid4().hex[:12]}"
    conn = db()
    try:
        recipients = ld.submit_homeowner_project(
            conn, lead_id, trade, region,
            lead_type=data.get("lead_type", "residential"),
            project_title=data.get("project_title"), city=data.get("city"))
        recips = [dict(r) for r in ld.get_distribution_log(conn, lead_id)]
    finally:
        conn.close()
    return jsonify(lead_id=lead_id, region=region,
                   distributed_to=len(recipients), recipients=recips), 201


@app.get("/api/leads/<lead_id>/distributions")
def lead_distributions(lead_id):
    conn = db()
    try:
        rows = ld.get_distribution_log(conn, lead_id)
    finally:
        conn.close()
    return jsonify(distributions=[dict(r) for r in rows])


# ── Pro: lead responses ──────────────────────────────────────────────────
@app.post("/api/leads/<lead_id>/accept")
def accept(lead_id):
    pro_id = (request.get_json(force=True, silent=True) or {}).get("pro_id")
    conn = db()
    try:
        changed = ld.accept_lead(conn, lead_id, pro_id)
    finally:
        conn.close()
    return jsonify(accepted=changed)


@app.post("/api/leads/<lead_id>/decline")
def decline(lead_id):
    data = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        result = ld.decline_lead(conn, lead_id, data.get("pro_id"),
                                 reason=data.get("reason"))
    finally:
        conn.close()
    return jsonify(result)


# ── Pro: accounts ────────────────────────────────────────────────────────
@app.post("/api/pro/signup")
def signup():
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        pro_id = accounts.create_pro_account(
            conn, name=d.get("name", ""), company=d.get("company"),
            email=d.get("email"), phone=d.get("phone"),
            password=d.get("password", ""), trades=d.get("trades", []),
            regions=d.get("regions", []), plan=d.get("plan"),
            coverage_type=d.get("coverage_type"))
        # Completing signup claims a Launch Partner seat (decrements the counter)
        # while seats remain. None means the offer is sold out -> normal pricing.
        claim = lp.claim_seat(conn, pro_id, apex_tier=d.get("apex_tier"))
        verify_token = accounts.get_pro(conn, pro_id)["email_verify_token"]
    except Exception as e:
        conn.close()
        return jsonify(error=str(e)), 400
    conn.close()
    # Email verification link (must be verified before login / dashboard access).
    site = (config.allowed_origins_list()[0] if isinstance(config.allowed_origins_list(), list)
            else "https://solaryienconnect.com")
    verify_url = f"{site}/pro/verify-email.html?token={verify_token}"
    try:
        emailer.send(d.get("email"), "Verify your Solaryien Connect email",
                     f"Welcome to Solaryien Connect. Please verify your email to activate "
                     f"your account and access your dashboard:\n\n{verify_url}\n\n— Solaryien Connect")
    except Exception as e:
        log.warning("Verification email failed: %s", e)
    # Launch Partner confirmation email (best-effort; never blocks signup).
    if claim:
        try:
            subj, body = lp.confirmation_email({"name": d.get("name")}, claim)
            emailer.send(d.get("email"), subj, body)
        except Exception as e:
            log.warning("Confirmation email failed: %s", e)
    # verify_url is also returned so the flow can complete before SMTP is configured.
    return jsonify(pro_id=pro_id, status="pending", launch_partner=claim,
                   verify_url=verify_url, email_verified=False), 201


def _form_list(form, key):
    """Read a list from multipart form: repeated fields or a JSON-array string."""
    vals = form.getlist(key)
    if len(vals) == 1 and vals[0].strip().startswith("["):
        try:
            return json.loads(vals[0])
        except Exception:
            return [vals[0]]
    return vals


# Files accepted at registration -> stored doc_type. Contractor license + the two
# workers'-comp options are conditional/either-or; the rest are always required.
_REGISTER_DOCS = {
    "gov_id": "gov_id",
    "insurance": "insurance",
    "business_license": "business_license",
    "contractor_license": "contractor_license",
    "wc_certificate": "wc_certificate",
    "wc_exclusion": "wc_exclusion",
}


@app.post("/api/pro/register")
def pro_register():
    """
    Single-flow contractor registration (multipart/form-data). The account is
    created here, at the END of the guided sign-up, only after every step is
    collected: account details, locked Apex tier, all required documents, and
    the dashboard/business profile. No payment is taken. Claims a free Launch
    Partner seat at the locked tier.

    Form fields: name, company, email, phone, password, apex_tier,
      trades (JSON or repeated), regions (JSON or repeated), years_in_business,
      website, bio, bg_sign_name, agreement_sign_name, tos_sign_name.
    Files: gov_id, insurance, business_license (required); contractor_license
      (conditional); exactly one of wc_certificate / wc_exclusion.
    """
    form = request.form
    apex_tier = (form.get("apex_tier") or "").strip().lower()
    if apex_tier not in lp.APEX_TIERS:
        return jsonify(error="A valid tier (starter/professional/enterprise) is required."), 400

    # Required documents present?
    files = request.files
    def _has(k):
        f = files.get(k)
        return bool(f and f.filename)
    missing = [k for k in ("gov_id", "insurance", "business_license") if not _has(k)]
    if missing:
        return jsonify(error="Missing required documents: " + ", ".join(missing)), 400
    if not (_has("wc_certificate") ^ _has("wc_exclusion")):
        return jsonify(error="Provide exactly one of: workers' comp certificate OR exclusion."), 400

    conn = db()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    try:
        trades = _form_list(form, "trades")
        regions = _form_list(form, "regions")
        pro_id = accounts.create_pro_account(
            conn, name=form.get("name", ""), company=form.get("company"),
            email=form.get("email"), phone=form.get("phone"),
            password=form.get("password", ""), trades=trades, regions=regions)
        onboarding.update_business_profile(
            conn, pro_id, trades=trades, regions=regions,
            years_in_business=(form.get("years_in_business") or None),
            website=form.get("website"), bio=form.get("bio"))
        for field, doc_type in _REGISTER_DOCS.items():
            f = files.get(field)
            if f and f.filename:
                onboarding.store_document(conn, pro_id, doc_type, f.filename,
                                          f.mimetype, f.read())
        for field, atype in (("bg_sign_name", "background_check_authorization"),
                             ("agreement_sign_name", "contractor_verification_agreement"),
                             ("tos_sign_name", "terms_of_service")):
            name = form.get(field)
            if name:
                onboarding.sign_agreement(conn, pro_id, atype, name, ip)
        # Free Launch Partner seat at the LOCKED tier.
        claim = lp.claim_seat(conn, pro_id, apex_tier=apex_tier)
        status = onboarding.submit_for_verification(conn, pro_id)
        verify_token = accounts.get_pro(conn, pro_id)["email_verify_token"]
    except Exception as e:
        conn.close()
        return jsonify(error=str(e)), 400
    conn.close()

    site = (config.allowed_origins_list()[0] if isinstance(config.allowed_origins_list(), list)
            else "https://solaryienconnect.com")
    verify_url = f"{site}/pro/verify-email.html?token={verify_token}"
    try:
        emailer.send(form.get("email"), "Verify your Solaryien Connect email",
                     f"Welcome to Solaryien Connect. Please verify your email to activate "
                     f"your account and access your dashboard:\n\n{verify_url}\n\n— Solaryien Connect")
    except Exception as e:
        log.warning("Verification email failed: %s", e)
    if claim:
        try:
            subj, body = lp.confirmation_email({"name": form.get("name")}, claim)
            emailer.send(form.get("email"), subj, body)
        except Exception as e:
            log.warning("LP confirmation email failed: %s", e)
    try:
        subj, body = onboarding.signup_confirmation_email(
            {"name": form.get("name")}, lp_claim=claim)
        emailer.send(form.get("email"), subj, body)
    except Exception as e:
        log.warning("Signup confirmation email failed: %s", e)

    return jsonify(pro_id=pro_id, status="pending", apex_tier=apex_tier,
                   verification_status=status, launch_partner=claim,
                   verify_url=verify_url, email_verified=False), 201


@app.get("/api/launch-partner")
def launch_partner_status():
    conn = db()
    try:
        return jsonify(lp.get_status(conn))
    finally:
        conn.close()


@app.get("/api/pro/<int:pro_id>/launch-partner")
def pro_launch_partner(pro_id):
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        return jsonify(claim=lp.get_claim(conn, pro_id))
    finally:
        conn.close()


@app.post("/api/pro/<int:pro_id>/apex-tier")
def pro_apex_tier(pro_id):
    """Record the Apex tier the pro chose for their Launch Partner trial (step 4)."""
    tier = (request.get_json(force=True, silent=True) or {}).get("apex_tier")
    conn = db()
    try:
        lp.set_apex_tier(conn, pro_id, tier)
    finally:
        conn.close()
    return jsonify(ok=True, apex_tier=tier)


@app.post("/api/pro/login")
def login():
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        row = accounts.authenticate(conn, d.get("email"), d.get("password", ""))
        if row is None:
            return jsonify(error="Invalid email or password"), 401
        if not row["email_verified"]:
            return jsonify(error="Please verify your email before logging in.",
                           email_unverified=True), 403
        token = auth.create_session(conn, "pro", row["id"])
        out = dict(token=token, account_type="pro", pro_id=row["id"], name=row["name"],
                   company=row["company"], status=row["status"], work_type=row["work_type"],
                   plan=row["plan"], coverage_type=row["coverage_type"],
                   verification_status=row["verification_status"])
    finally:
        conn.close()
    return jsonify(out)


@app.get("/api/verify-email")
@app.post("/api/verify-email")
def verify_email():
    token = request.args.get("token") or (request.get_json(force=True, silent=True) or {}).get("token")
    conn = db()
    try:
        ok = accounts.verify_email(conn, token)
    finally:
        conn.close()
    return jsonify(verified=ok), (200 if ok else 400)


@app.post("/api/pro/<int:pro_id>/subscribe")
def subscribe_dev(pro_id):
    """Dev/manual activation without Stripe (e.g. comped accounts)."""
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        row = accounts.approve_and_activate(
            conn, pro_id, plan=d.get("plan"), coverage_type=d.get("coverage_type"))
    finally:
        conn.close()
    return jsonify(pro_id=pro_id, status=row["status"],
                   in_good_standing=row["in_good_standing"])


# ── Onboarding: business profile, documents, verification, complete ──────
@app.post("/api/pro/<int:pro_id>/business")
def pro_business(pro_id):
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        onboarding.update_business_profile(
            conn, pro_id, trades=d.get("trades"), regions=d.get("regions"),
            years_in_business=d.get("years_in_business"), website=d.get("website"),
            bio=d.get("bio"))
    finally:
        conn.close()
    return jsonify(ok=True)


@app.post("/api/pro/<int:pro_id>/verification")
def pro_verification_submit(pro_id):
    """
    Step 3: multipart submission. Files: coi (required), license (optional).
    Form fields: bg_sign_name, agreement_sign_name, tos_sign_name (digital signatures).
    Flags the account Pending Verification when COI + all agreements are in.
    """
    conn = db()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    try:
        for key, doc_type in (("coi", "coi"), ("license", "license")):
            f = request.files.get(key)
            if f and f.filename:
                onboarding.store_document(conn, pro_id, doc_type, f.filename,
                                          f.mimetype, f.read())
        form = request.form
        sig_map = {
            "bg_sign_name": "background_check_authorization",
            "agreement_sign_name": "contractor_verification_agreement",
            "tos_sign_name": "terms_of_service",
        }
        for field, atype in sig_map.items():
            name = form.get(field)
            if name:
                onboarding.sign_agreement(conn, pro_id, atype, name, ip)
        status = onboarding.submit_for_verification(conn, pro_id)
        checklist = onboarding.verification_checklist(conn, pro_id)
    finally:
        conn.close()
    return jsonify(verification_status=status, checklist=checklist)


@app.post("/api/pro/<int:pro_id>/complete")
def pro_complete(pro_id):
    """Step 5: record chosen plan and send the confirmation email."""
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        if d.get("plan"):
            conn.execute("UPDATE pros SET plan = ?, coverage_type = ? WHERE id = ?",
                         (d.get("plan"), d.get("coverage_type"), pro_id))
            conn.commit()
        pro = accounts.get_pro(conn, pro_id)
        claim = lp.get_claim(conn, pro_id)
    finally:
        conn.close()
    if pro:
        try:
            subj, body = onboarding.signup_confirmation_email(
                dict(pro), plan=d.get("plan"), lp_claim=claim)
            emailer.send(pro["email"], subj, body)
        except Exception as e:
            log.warning("Confirmation email failed: %s", e)
    return jsonify(ok=True, verification_status=pro["verification_status"] if pro else None)


@app.get("/api/pro/<int:pro_id>/me")
def pro_me(pro_id):
    """Authenticated pro's own profile — the dashboard calls this to gate access."""
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        p = accounts.get_pro(conn, pro_id)
    finally:
        conn.close()
    if not p:
        return _deny()
    return jsonify(pro_id=p["id"], name=p["name"], company=p["company"], status=p["status"],
                   work_type=p["work_type"], plan=p["plan"], coverage_type=p["coverage_type"],
                   verification_status=p["verification_status"], email_verified=bool(p["email_verified"]))


@app.get("/api/pro/<int:pro_id>/leads")
def pro_leads(pro_id):
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        rows = ld.get_pro_leads(conn, pro_id)
    finally:
        conn.close()
    return jsonify(leads=[dict(r) for r in rows])


@app.get("/api/pros/lead-counts")
def pro_lead_counts():
    conn = db()
    try:
        rows = ld.get_pro_lead_counts(conn)
    finally:
        conn.close()
    return jsonify(pros=[dict(r) for r in rows])


# ── Public Pro Directory (no auth) ───────────────────────────────────────
def _pro_public_dict(conn, p):
    trades = [r["trade"] for r in conn.execute(
        "SELECT trade FROM pro_trades WHERE pro_id = ?", (p["id"],)).fetchall()]
    regions = [r["region_code"] for r in conn.execute(
        "SELECT region_code FROM pro_regions WHERE pro_id = ?", (p["id"],)).fetchall()]
    return {
        "pro_id": p["id"], "company": p["company"] or p["name"], "name": p["name"],
        "trades": trades, "regions": regions,
        "work_type": p["work_type"] or "both",
        "bio": p["bio"], "website": p["website"],
        "years_in_business": p["years_in_business"],
        # Listed pros are active (vetted + live), so they carry the Verified badge.
        "verified": (p["status"] == "active") or (p["verification_status"] == "approved"),
        # No reviews system yet — surfaced as "New" on the card.
        "rating": None, "review_count": 0,
    }


@app.get("/api/pros/directory")
def pros_directory():
    """Public, filterable list of active/verified pros for the Pro Directory."""
    trade = request.args.get("trade")
    region = request.args.get("region")
    work = (request.args.get("work_type") or "").lower()   # residential|commercial|both
    q = (request.args.get("q") or "").strip().lower()
    conn = db()
    try:
        rows = conn.execute("SELECT * FROM pros WHERE status = 'active'").fetchall()
        out = []
        for p in rows:
            d = _pro_public_dict(conn, p)
            if trade and trade not in d["trades"]:
                continue
            if region and region not in d["regions"]:
                continue
            if work and work != "both" and d["work_type"] not in (work, "both"):
                continue
            if q and q not in (d["company"] or "").lower() and q not in (d["name"] or "").lower():
                continue
            out.append(d)
    finally:
        conn.close()
    return jsonify(pros=out)


@app.get("/api/pros/<int:pro_id>/public")
def pro_public(pro_id):
    """Public profile for a single pro (Pro Directory detail page)."""
    conn = db()
    try:
        p = accounts.get_pro(conn, pro_id)
        if not p or p["status"] != "active":
            return jsonify(error="not found"), 404
        d = _pro_public_dict(conn, p)
    finally:
        conn.close()
    return jsonify(pro=d)


@app.post("/api/pros/<int:pro_id>/quote")
def pro_quote_request(pro_id):
    """Homeowner/public quote request to a pro (no login). Emails the pro."""
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        p = accounts.get_pro(conn, pro_id)
    finally:
        conn.close()
    if not p:
        return jsonify(error="not found"), 404
    try:
        emailer.send(p["email"], "New quote request from Solaryien Connect",
                     f"Hi {p['name']},\n\nYou have a new quote request via your Solaryien "
                     f"Connect directory profile:\n\n"
                     f"  Name:  {d.get('name','')}\n"
                     f"  Email: {d.get('email','')}\n"
                     f"  Phone: {d.get('phone','')}\n"
                     f"  Project: {d.get('message','')}\n\n— Solaryien Connect")
    except Exception as e:
        log.warning("Quote request email failed: %s", e)
    return jsonify(ok=True)


# ── Pro profile edit (authenticated — used by the dashboard) ─────────────
@app.get("/api/pro/<int:pro_id>/profile")
def pro_profile_get(pro_id):
    """The pro's own editable profile (prefill for the dashboard editor)."""
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        p = accounts.get_pro(conn, pro_id)
        if not p:
            return _deny()
        d = _pro_public_dict(conn, p)
        d["plan"] = p["plan"]
        d["coverage_type"] = p["coverage_type"]
        d["email"] = p["email"]
        d["status"] = p["status"]
        d["verification_status"] = p["verification_status"]
    finally:
        conn.close()
    return jsonify(profile=d)


@app.post("/api/pro/<int:pro_id>/profile")
def pro_profile_update(pro_id):
    """Update the pro's own profile (display name, bio, trades, regions, etc.)."""
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        d = request.get_json(force=True, silent=True) or {}
        if d.get("company"):
            conn.execute("UPDATE pros SET company = ? WHERE id = ?", (d["company"], pro_id))
        if d.get("work_type") in ("residential", "commercial", "both"):
            conn.execute("UPDATE pros SET work_type = ? WHERE id = ?", (d["work_type"], pro_id))
        conn.commit()
        onboarding.update_business_profile(
            conn, pro_id, trades=d.get("trades"), regions=d.get("regions"),
            years_in_business=d.get("years_in_business"), bio=d.get("bio"))
    finally:
        conn.close()
    return jsonify(ok=True)


# ── Stripe subscription payments ─────────────────────────────────────────
@app.post("/api/pro/<int:pro_id>/checkout-session")
def checkout_session(pro_id):
    if not stripe_integration.configured():
        return jsonify(error="Stripe is not configured on this server"), 503
    d = request.get_json(force=True, silent=True) or {}
    try:
        url = stripe_integration.create_checkout_session(
            pro_id, d.get("plan"), coverage_type=d.get("coverage_type"),
            customer_email=d.get("email"))
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(checkout_url=url)


@app.get("/api/stripe/config")
def stripe_config():
    """Expose the publishable key for the embedded modal (safe to share)."""
    return jsonify(publishable_key=stripe_integration.publishable_key() or "",
                   configured=stripe_integration.configured())


@app.post("/api/checkout/session")
def checkout_embedded_session():
    """
    Embedded Stripe Checkout for any payment touchpoint (post-launch
    subscription, the $50 background check, add-on seats). Returns a
    client_secret the in-page modal mounts. Body: {price_key, pro_id?, email?,
    quantity?}.
    """
    if not stripe_integration.configured():
        return jsonify(error="Stripe is not configured on this server"), 503
    d = request.get_json(force=True, silent=True) or {}
    try:
        secret = stripe_integration.create_embedded_session(
            d.get("price_key"), pro_id=d.get("pro_id"),
            customer_email=d.get("email"), quantity=int(d.get("quantity", 1) or 1))
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(client_secret=secret)


@app.post("/api/stripe/webhook")
def stripe_webhook():
    if not stripe_integration.configured():
        return ("", 503)
    try:
        result = stripe_integration.handle_webhook(
            request.data, request.headers.get("Stripe-Signature"))
    except Exception as e:
        log.warning("Stripe webhook verification failed: %s", e)
        return ("", 400)
    if result and result.get("activate_pro_id"):
        conn = db()
        try:
            accounts.approve_and_activate(
                conn, result["activate_pro_id"],
                plan=result.get("plan"), coverage_type=result.get("coverage_type"))
            log.info("Activated pro %s after Stripe payment", result["activate_pro_id"])
        finally:
            conn.close()
    return ("", 200)


# ── Solaryien Apex inbound webhook ───────────────────────────────────────
@app.post("/api/apex/webhook")
def apex_webhook():
    """
    Receive a lead/event from Solaryien Apex. Validates an HMAC-SHA256
    signature over the raw body using APEX_WEBHOOK_SECRET, then ingests the
    lead and runs distribution. Idempotent on lead_id.
    """
    raw = request.data
    secret = config.APEX_WEBHOOK_SECRET
    if secret:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        sig = request.headers.get("X-Apex-Signature", "")
        if not hmac.compare_digest(expected, sig):
            return jsonify(error="invalid signature"), 401

    data = json.loads(raw or b"{}")
    project = data.get("project", data)
    lead_id = data.get("lead_id") or f"apex_{uuid.uuid4().hex[:12]}"
    trade = project.get("trade_category") or project.get("trade")
    region = project.get("region_code") or regions.region_for_zip(project.get("zip_code"))
    if not trade or not region:
        return jsonify(error="trade_category and region_code are required"), 400

    conn = db()
    try:
        exists = conn.execute("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if exists:  # idempotent — Apex may retry
            return jsonify(lead_id=lead_id, status="duplicate"), 200
        recipients = ld.submit_homeowner_project(
            conn, lead_id, trade, region,
            lead_type=data.get("lead_type", "commercial"),
            project_title=project.get("title"), city=project.get("city"))
    finally:
        conn.close()
    return jsonify(lead_id=lead_id, distributed_to=len(recipients)), 201


# ═══ COMMERCIAL PLATFORM API ═════════════════════════════════════════════
@app.get("/commercial/api/csi")
def commercial_csi():
    return jsonify(divisions=[{"code": c, "name": n, "label": csi.label(c)}
                              for c, n in csi.CSI_DIVISIONS.items()])


@app.post("/commercial/api/owner/signup")
def owner_signup():
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        oid = po.create_owner(
            conn, first_name=d.get("first_name", ""), last_name=d.get("last_name", ""),
            company_name=d.get("company_name", ""), title=d.get("title"),
            email=d.get("email"), phone=d.get("phone"), password=d.get("password", ""))
    except Exception as e:
        conn.close()
        return jsonify(error=str(e)), 400
    conn.close()
    return jsonify(owner_id=oid), 201


@app.post("/commercial/api/owner/login")
def owner_login():
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        row = po.authenticate(conn, d.get("email"), d.get("password", ""))
        if not row:
            return jsonify(error="Invalid email or password"), 401
        token = auth.create_session(conn, "owner", row["id"])
        out = dict(token=token, account_type="owner", owner_id=row["id"],
                   first_name=row["first_name"], company_name=row["company_name"])
    finally:
        conn.close()
    return jsonify(out)


@app.post("/commercial/api/login")
def commercial_login():
    """Combined login — detects Pro vs Project Owner."""
    d = request.get_json(force=True, silent=True) or {}
    email, pw = d.get("email"), d.get("password", "")
    conn = db()
    try:
        pro = accounts.authenticate(conn, email, pw)
        if pro:
            if not pro["email_verified"]:
                return jsonify(error="Please verify your email before logging in.",
                               email_unverified=True), 403
            token = auth.create_session(conn, "pro", pro["id"])
            return jsonify(token=token, account_type="pro", pro_id=pro["id"], name=pro["name"],
                           company=pro["company"], work_type=pro["work_type"],
                           plan=pro["plan"], coverage_type=pro["coverage_type"])
        owner = po.authenticate(conn, email, pw)
        if owner:
            token = auth.create_session(conn, "owner", owner["id"])
            return jsonify(token=token, account_type="owner", owner_id=owner["id"],
                           first_name=owner["first_name"], company_name=owner["company_name"])
    finally:
        conn.close()
    return jsonify(error="Invalid email or password"), 401


@app.get("/commercial/api/projects")
def commercial_projects_list():
    conn = db()
    try:
        rows = cm.list_projects(conn, region=request.args.get("region"))
        out = [cm.project_detail(conn, r["project_uid"]) for r in rows]
    finally:
        conn.close()
    return jsonify(projects=out)


@app.get("/commercial/api/projects/<uid>")
def commercial_project_detail(uid):
    conn = db()
    try:
        d = cm.project_detail(conn, uid, count_view=True)
    finally:
        conn.close()
    if not d:
        return jsonify(error="not found"), 404
    return jsonify(project=d)


@app.post("/commercial/api/projects")
def commercial_project_create():
    d = request.get_json(force=True, silent=True) or {}
    owner_id = d.get("owner_id")
    if not owner_id:
        return jsonify(error="owner_id required (log in as a Project Owner)"), 401
    conn = db()
    try:
        res = cm.post_project(conn, owner_id, d)
    except ValueError as e:
        conn.close()
        return jsonify(error=str(e)), 400
    conn.close()
    return jsonify(res), 201


@app.post("/commercial/api/projects/<uid>/files")
def commercial_project_files(uid):
    conn = db()
    try:
        proj = cm.get_project_row(conn, uid)
        if not proj:
            return jsonify(error="not found"), 404
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify(error="file required"), 400
        content = f.read()
        conn.execute(
            "INSERT INTO project_files (project_id, file_name, mime, file_type, file_size, "
            "content, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (proj["id"], f.filename, f.mimetype, request.form.get("file_type", "other"),
             len(content), content, request.form.get("owner_id")))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True), 201


@app.get("/commercial/api/projects/<uid>/bids")
def commercial_project_bids(uid):
    conn = db()
    try:
        proj = cm.get_project_row(conn, uid)
        if not proj:
            return jsonify(error="not found"), 404
        rows = cm.list_bids(conn, proj["id"])
        out = [{k: r[k] for k in r.keys() if k != "bid_file_content"} for r in rows]
    finally:
        conn.close()
    return jsonify(bids=out)


@app.post("/commercial/api/projects/<uid>/bids")
def commercial_submit_bid(uid):
    # multipart (optional PDF) or JSON
    if request.content_type and "multipart" in request.content_type:
        form = request.form
        pro_id = form.get("pro_id")
        f = request.files.get("bid_file")
        fname = f.filename if f and f.filename else None
        fcontent = f.read() if f and f.filename else None
        amount, scope = form.get("bid_amount"), form.get("scope_of_work")
    else:
        d = request.get_json(force=True, silent=True) or {}
        pro_id, amount, scope = d.get("pro_id"), d.get("bid_amount"), d.get("scope_of_work")
        fname = fcontent = None
    conn = db()
    try:
        pro = accounts.get_pro(conn, pro_id)
        if not pro or not cm.has_commercial_access(conn, pro):
            return jsonify(error="commercial subscription required"), 403
        cm.submit_bid(conn, uid, pro_id, float(amount), scope, fname, fcontent)
    except ValueError as e:
        conn.close()
        return jsonify(error=str(e)), 400
    conn.close()
    return jsonify(ok=True), 201


@app.post("/commercial/api/bids/<int:bid_id>/withdraw")
def commercial_withdraw_bid(bid_id):
    pro_id = (request.get_json(force=True, silent=True) or {}).get("pro_id")
    conn = db()
    try:
        ok = cm.withdraw_bid(conn, bid_id, pro_id)
    finally:
        conn.close()
    return jsonify(ok=ok)


@app.put("/commercial/api/bids/<int:bid_id>/win")
def commercial_mark_win(bid_id):
    owner_id = (request.get_json(force=True, silent=True) or {}).get("owner_id")
    conn = db()
    try:
        ok = cm.mark_win(conn, bid_id, owner_id)
    finally:
        conn.close()
    return jsonify(ok=ok)


@app.post("/commercial/api/projects/<uid>/invite")
def commercial_invite(uid):
    d = request.get_json(force=True, silent=True) or {}
    conn = db()
    try:
        ok = cm.invite_contractor(conn, uid, d.get("pro_id"), d.get("owner_id"))
    finally:
        conn.close()
    return jsonify(ok=ok)


@app.get("/commercial/api/owner/<int:owner_id>/projects")
def commercial_owner_projects(owner_id):
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "owner", owner_id):
            return _deny()
        rows = conn.execute("SELECT * FROM commercial_projects WHERE owner_id = ? "
                            "ORDER BY created_at DESC", (owner_id,)).fetchall()
        out = [cm.project_detail(conn, r["project_uid"]) for r in rows]
    finally:
        conn.close()
    return jsonify(projects=out)


@app.get("/commercial/api/pro/<int:pro_id>/projects")
def commercial_pro_feed(pro_id):
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        return jsonify(projects=cm.pro_commercial_projects(conn, pro_id))
    finally:
        conn.close()


@app.get("/commercial/api/pro/<int:pro_id>/bids")
def commercial_pro_bids(pro_id):
    conn = db()
    try:
        if not auth.authorized(conn, _bearer(), "pro", pro_id):
            return _deny()
        rows = cm.pro_bids(conn, pro_id)
        out = [{k: r[k] for k in r.keys() if k != "bid_file_content"} for r in rows]
    finally:
        conn.close()
    return jsonify(bids=out)


@app.get("/commercial/api/contractors")
def commercial_contractor_search():
    """Contractor directory for Project Owners (filter by trade/region)."""
    trade, region = request.args.get("trade"), request.args.get("region")
    conn = db()
    try:
        out = []
        for pro in conn.execute(
                "SELECT * FROM pros WHERE status='active' AND work_type IN ('commercial','both')"
        ).fetchall():
            if not cm.has_commercial_access(conn, pro):
                continue
            regs = cm._pro_regions(conn, pro["id"])
            trades = cm._pro_trades(conn, pro["id"])
            if region and region not in regs:
                continue
            if trade and trade not in trades:
                continue
            out.append({"pro_id": pro["id"], "company": pro["company"], "name": pro["name"],
                        "trades": trades, "regions": regs,
                        "verified": pro["verification_status"] == "approved"})
    finally:
        conn.close()
    return jsonify(contractors=out)


@app.route("/commercial/api/projects/<uid>/messages", methods=["GET", "POST"])
def commercial_messages(uid):
    conn = db()
    try:
        if request.method == "POST":
            d = request.get_json(force=True, silent=True) or {}
            cm.add_message(conn, uid, d.get("sender_type"), d.get("sender_id"), d.get("message_text"))
            return jsonify(ok=True), 201
        st, sid = request.args.get("sender_type"), request.args.get("sender_id")
        rows = cm.get_messages(conn, uid, st, int(sid) if sid else None)
        return jsonify(messages=[dict(r) for r in rows])
    finally:
        conn.close()


# ── Admin (Solaryien, Inc. verification review) ──────────────────────────
def _admin_ok():
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    return bool(config.ADMIN_PASSWORD) and token == config.ADMIN_PASSWORD


@app.post("/api/admin/login")
def admin_login():
    pw = (request.get_json(force=True, silent=True) or {}).get("password")
    if pw and pw == config.ADMIN_PASSWORD:
        return jsonify(token=config.ADMIN_PASSWORD)
    return jsonify(error="Invalid admin password"), 401


@app.get("/api/admin/pending")
def admin_pending():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        return jsonify(pending=onboarding.list_pending(conn))
    finally:
        conn.close()


@app.get("/api/admin/pro/<int:pro_id>/document/<doc_type>")
def admin_document(pro_id, doc_type):
    if not _admin_ok():
        return ("unauthorized", 401)
    conn = db()
    try:
        row = onboarding.get_document(conn, pro_id, doc_type)
    finally:
        conn.close()
    if not row:
        return ("not found", 404)
    from flask import Response
    return Response(row["content"], mimetype=row["mime"] or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{row["filename"]}"'})


@app.post("/api/admin/pro/<int:pro_id>/approve")
def admin_approve(pro_id):
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        onboarding.approve(conn, pro_id)
        pro = accounts.get_pro(conn, pro_id)
    finally:
        conn.close()
    if pro:
        try:
            emailer.send(pro["email"], "You're verified — welcome to Solaryien Connect",
                         f"Hi {pro['name']},\n\nYour Solaryien Connect account has been "
                         f"verified and is now active. You'll start receiving leads matched "
                         f"to your trade and service area.\n\n— Solaryien Connect")
        except Exception:
            pass
    return jsonify(ok=True, status="approved")


@app.post("/api/admin/pro/<int:pro_id>/reject")
def admin_reject(pro_id):
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    reason = (request.get_json(force=True, silent=True) or {}).get("reason")
    conn = db()
    try:
        onboarding.reject(conn, pro_id, reason)
        pro = accounts.get_pro(conn, pro_id)
    finally:
        conn.close()
    if pro:
        try:
            emailer.send(pro["email"], "Action needed on your Solaryien Connect application",
                         f"Hi {pro['name']},\n\nWe couldn't verify your account yet"
                         f"{(': ' + reason) if reason else ''}. Please review and resubmit "
                         f"your documents from your account.\n\n— Solaryien Connect")
        except Exception:
            pass
    return jsonify(ok=True, status="rejected")


# ── Admin commercial oversight ───────────────────────────────────────────
@app.get("/api/admin/commercial/projects")
def admin_comm_projects():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        rows = conn.execute("SELECT * FROM commercial_projects ORDER BY created_at DESC").fetchall()
        out = [cm.project_detail(conn, r["project_uid"]) for r in rows]
    finally:
        conn.close()
    return jsonify(projects=out)


@app.get("/api/admin/commercial/bids")
def admin_comm_bids():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        rows = conn.execute(
            "SELECT b.id, b.bid_amount, b.status, b.submitted_at, c.project_name, "
            "p.company AS pro_company FROM commercial_bids b "
            "JOIN commercial_projects c ON c.id=b.project_id "
            "JOIN pros p ON p.id=b.pro_id ORDER BY b.submitted_at DESC").fetchall()
    finally:
        conn.close()
    return jsonify(bids=[dict(r) for r in rows])


@app.get("/api/admin/commercial/owners")
def admin_comm_owners():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        rows = conn.execute("SELECT id, first_name, last_name, company_name, email, phone, "
                            "is_active, created_at FROM project_owner_accounts "
                            "ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return jsonify(owners=[dict(r) for r in rows])


@app.post("/api/admin/commercial/projects/<uid>/remove")
def admin_comm_remove(uid):
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = db()
    try:
        conn.execute("UPDATE commercial_projects SET status='cancelled', is_public=0 "
                     "WHERE project_uid=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
