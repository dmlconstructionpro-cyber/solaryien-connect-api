"""Environment-driven configuration (12-factor). All secrets come from env vars."""

import os


def _env(key, default=None):
    return os.environ.get(key, default)


# Where SQLite lives. On Render, point this at a mounted persistent disk
# (e.g. /var/data/solaryien_connect.db) so data survives restarts/deploys.
DB_PATH = _env("DATABASE_PATH", "solaryien_connect.db")

# Port Render injects.
PORT = int(_env("PORT", "5050"))

# CORS: comma-separated list of allowed origins, or "*" for any.
ALLOWED_ORIGINS = _env("ALLOWED_ORIGINS", "*")

# Seed verified demo pros on first boot if the DB is empty.
SEED_ON_START = _env("SEED_ON_START", "false").lower() in ("1", "true", "yes")

# ── Stripe ───────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
# Publishable key — safe to expose; the embedded modal uses it client-side.
STRIPE_PUBLISHABLE_KEY = _env("STRIPE_PUBLISHABLE_KEY")

# Map price key -> Stripe Price ID. Create these in Stripe (recurring annual for
# subscriptions; one-time for bg_check + seat) and set the IDs as env vars.
# Keys match the `purpose`/`price_key` sent by the embedded checkout modal.
STRIPE_PRICES = {
    # Apex standalone (annual)
    "apex_starter":        _env("STRIPE_PRICE_APEX_STARTER"),
    "apex_professional":   _env("STRIPE_PRICE_APEX_PROFESSIONAL"),
    "apex_enterprise":     _env("STRIPE_PRICE_APEX_ENTERPRISE"),
    # Connect standalone (annual)
    "connect_standard_res": _env("STRIPE_PRICE_CONNECT_STANDARD_RES"),
    "connect_standard_com": _env("STRIPE_PRICE_CONNECT_STANDARD_COM"),
    "connect_pro_res":      _env("STRIPE_PRICE_CONNECT_PRO_RES"),
    "connect_pro_com":      _env("STRIPE_PRICE_CONNECT_PRO_COM"),
    # Complete bundle (annual)
    "complete_standard":   _env("STRIPE_PRICE_COMPLETE_STANDARD"),
    "complete_pro":        _env("STRIPE_PRICE_COMPLETE_PRO"),
    "complete_enterprise": _env("STRIPE_PRICE_COMPLETE_ENTERPRISE"),
    # One-time charges
    "bg_check":            _env("STRIPE_PRICE_BG_CHECK"),     # $50 background check
    "seat":                _env("STRIPE_PRICE_SEAT"),         # $1,000 add-on seat / yr
    # Back-compat aliases for the legacy /checkout-session endpoint.
    "standard":            _env("STRIPE_PRICE_CONNECT_STANDARD_RES"),
    "pro":                 _env("STRIPE_PRICE_CONNECT_PRO_RES"),
    "complete":            _env("STRIPE_PRICE_COMPLETE_STANDARD"),
}
# One-time (not subscription) price keys — used to pick Stripe checkout mode.
STRIPE_ONE_TIME = {"bg_check", "seat"}

CHECKOUT_SUCCESS_URL = _env("CHECKOUT_SUCCESS_URL",
                            "https://solaryienconnect.com/pro/confirmation.html")
CHECKOUT_CANCEL_URL = _env("CHECKOUT_CANCEL_URL",
                           "https://solaryienconnect.com/pro/subscribe.html")
# Embedded Checkout returns here with {CHECKOUT_SESSION_ID}; the page confirms status.
CHECKOUT_RETURN_URL = _env("CHECKOUT_RETURN_URL",
                           "https://solaryienconnect.com/pro/confirmation.html"
                           "?session_id={CHECKOUT_SESSION_ID}")

# ── Solaryien Apex inbound webhook ───────────────────────────────────────
# Shared secret used to validate the HMAC-SHA256 signature on incoming
# webhook calls from Solaryien Apex.
APEX_WEBHOOK_SECRET = _env("APEX_WEBHOOK_SECRET")

# ── Email (confirmation + conversion reminders) ──────────────────────────
# Sender for now; switch to a dedicated SolaryienConnect.com address later by
# setting EMAIL_FROM in the environment. Sending is skipped (logged only) until
# SMTP_HOST is configured, so the app still runs without an email provider.
EMAIL_FROM = _env("EMAIL_FROM", "Circe-lilitu@solaryien.com")

# Admin review page password (set a strong value in the environment).
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "change-me-admin")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = _env("SMTP_PASS")


def allowed_origins_list():
    if ALLOWED_ORIGINS == "*":
        return "*"
    return [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
