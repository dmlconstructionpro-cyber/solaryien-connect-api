# Deploying the Solaryien Connect backend to Render

The backend is a Flask API. This guide takes it live on Render and connects it
to the SolaryienConnect.com frontend. Steps that need *your* accounts (Render,
Stripe, domain DNS) are called out — those can't be automated for you.

Estimated time: ~15 minutes.

---

## 0. What you need
- A **GitHub** (or GitLab) account
- A **Render** account — https://render.com
- A **Stripe** account (for subscription payments)
- Access to the **SolaryienConnect.com DNS** (your domain registrar / host)

---

## 1. Put the backend in a Git repo
Render deploys from Git. Create a new repo and push the `backend/` folder.

```bash
cd connect_website/backend
git init
git add .
git commit -m "Solaryien Connect API"
git branch -M main
git remote add origin https://github.com/<you>/solaryien-connect-api.git
git push -u origin main
```

`.gitignore` already excludes `*.db`, `__pycache__`, and `.env`, so no secrets
or local data are committed.

---

## 2. Deploy on Render (Blueprint)
1. Render dashboard → **New + → Blueprint**.
2. Connect the repo from step 1. Render reads **`render.yaml`** and proposes a
   web service `solaryien-connect-api`.
   - If the backend is the repo root (it is, above), **delete the `rootDir: backend`
     line** in `render.yaml` first, or set it to `.`.
3. Click **Apply**. Render runs `pip install -r requirements.txt` and starts it
   with `gunicorn app:app`. Health check: `/healthz`.
4. **Persistent data:** `render.yaml` attaches a 1 GB disk at `/var/data` and
   points `DATABASE_PATH` there so leads/pros survive restarts. Disks require a
   **Starter** instance (paid). On the free tier, remove the `disk:` block and the
   `DATABASE_PATH` env var — but note SQLite then resets on each deploy.

When it goes live you'll get a URL like
`https://solaryien-connect-api.onrender.com`. Copy it.

---

## 3. Stripe (subscription payments)
1. Stripe dashboard → **Products** → create a product per plan (Standard, Pro,
   Complete) with a **recurring price**. Copy each **Price ID** (`price_...`).
2. Render → your service → **Environment** → set:
   - `STRIPE_SECRET_KEY` = your `sk_live_...` (or `sk_test_...` to test)
   - `STRIPE_PRICE_STANDARD`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_COMPLETE` = the Price IDs
3. Stripe → **Developers → Webhooks → Add endpoint**:
   - URL: `https://<your-render-url>/api/stripe/webhook`
   - Event: `checkout.session.completed`
   - Copy the **Signing secret** (`whsec_...`) → set `STRIPE_WEBHOOK_SECRET` in Render.

On successful payment the webhook activates the pro (status → `active`, eligible
for leads). Without these vars the app still runs; checkout returns 503.

---

## 4. Point the frontend at the backend
Edit **`connect_website/api.js`** and set your Render URL:

```js
API_BASE: "https://solaryien-connect-api.onrender.com",
```

Until this is set (it ships as `REPLACE_ME`), the site runs in demo mode and
forms fall back to local success. After setting it, re-upload the site files to
your web host. The wired forms: Post a Project, Pro signup, Pro login, Pro
dashboard (leads + accept/decline), and Subscribe (Stripe checkout).

`ALLOWED_ORIGINS` in `render.yaml` is already set to
`https://solaryienconnect.com,https://www.solaryienconnect.com` so the browser
can call the API. Add any other origins you serve the site from.

---

## 5. Solaryien Apex webhook
Render auto-generates `APEX_WEBHOOK_SECRET` (see the service's Environment tab).
Configure Apex to POST leads to:

```
POST https://<your-render-url>/api/apex/webhook
Header: X-Apex-Signature: <hex HMAC-SHA256 of the raw body using APEX_WEBHOOK_SECRET>
Body:  { "lead_id": "...", "project": { "trade_category": "...", "region_code": "R5", ... } }
```

Invalid signatures are rejected (401); duplicate `lead_id`s are ignored (idempotent).

---

## 6. (Optional) API on a custom domain
Render → service → **Settings → Custom Domains** → add `api.solaryienconnect.com`,
then add the CNAME it shows at your DNS host. Update `api.js` `API_BASE` to match.

---

## Local development
```bash
cd backend
pip install -r requirements.txt
python seed_pros.py            # load verified demo pros
python app.py                  # http://localhost:5050
# then in api.js: API_BASE = "http://localhost:5050"
```

## Tests
```bash
python test_distribution.py    # distribution rules + fair rotation
python test_lifecycle.py       # signup -> verify -> activate, accept/decline
```

## Environment variables (summary)
| Var | Purpose |
|-----|---------|
| `DATABASE_PATH` | SQLite path (point at the mounted disk on Render) |
| `ALLOWED_ORIGINS` | CORS allow-list, comma-separated, or `*` |
| `SEED_ON_START` | `true` seeds demo pros on first boot if empty |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe auth + webhook verify |
| `STRIPE_PRICE_STANDARD` / `_PRO` / `_COMPLETE` | Stripe Price IDs per plan |
| `CHECKOUT_SUCCESS_URL` / `CHECKOUT_CANCEL_URL` | Stripe redirect targets |
| `APEX_WEBHOOK_SECRET` | HMAC secret for the Apex inbound webhook |
