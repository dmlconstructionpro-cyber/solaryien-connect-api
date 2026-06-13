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
# Map plan -> Stripe Price ID (create these as recurring prices in Stripe).
STRIPE_PRICES = {
    "standard": _env("STRIPE_PRICE_STANDARD"),
    "pro":      _env("STRIPE_PRICE_PRO"),
    "complete": _env("STRIPE_PRICE_COMPLETE"),
}
CHECKOUT_SUCCESS_URL = _env("CHECKOUT_SUCCESS_URL",
                            "https://solaryienconnect.com/pro/confirmation.html")
CHECKOUT_CANCEL_URL = _env("CHECKOUT_CANCEL_URL",
                           "https://solaryienconnect.com/pro/subscribe.html")

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
