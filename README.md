# Solaryien Connect — Lead Distribution Backend

When a homeowner submits a project, the system selects **3–5 verified pros** to
receive it. Pros do **not** receive every lead in their area — they receive
leads **matched to their trade and service area, distributed fairly among
verified pros.**

## Files

| File | Purpose |
|------|---------|
| `database.py` | SQLite schema + connection helpers (stdlib only) |
| `lead_distribution.py` | The selection engine — all rules below (stdlib only) |
| `app.py` | Minimal Flask API the `Post a Project` form POSTs to |
| `test_distribution.py` | Runnable proof of every rule — `python test_distribution.py` |

The engine has **no third-party dependencies**, so it runs and tests anywhere
Python is installed. Flask is only needed for the HTTP layer (`app.py`).

## The rules → where they live

| # | Rule | Implementation |
|---|------|----------------|
| 1 | Pro must be **subscribed to the region** | `pro_regions` join in `eligible_pros()` |
| 2 | Pro must **offer the trade** | `pro_trades` join in `eligible_pros()` (trade normalized) |
| 3 | Pro must be **active and in good standing** | `pros.status = 'active' AND in_good_standing = 1` |
| 4 | **Rotate fairly** — prefer pros with fewer recent leads | `ORDER BY recent_leads, total_leads, last_lead_at` |
| 5 | **Never** send the same lead to the same pro twice | `UNIQUE(lead_id, pro_id)` + `NOT IN (...)` filter |
| 6 | **Log** every distribution | `lead_distributions` rows + `logging` |

`recent_leads` is counted from `lead_distributions` inside a rolling
`ROTATION_WINDOW_DAYS` window (default 30), so fairness self-corrects over time
even as older activity ages out. `pro_lead_count` keeps the lifetime running
total per pro for fast tie-breaks and reporting.

## Required tables

- **`lead_distributions`** — one row per (lead, pro): `lead_id`, `pro_id`,
  `distributed_at`, `status`. `UNIQUE(lead_id, pro_id)` enforces "never twice."
  This table **is** the audit log.
- **`pro_lead_count`** — `pro_id`, `total_leads`, `last_lead_at`, `updated_at`.
  Running totals per pro, used for fair rotation.

Supporting tables (`pros`, `pro_trades`, `pro_regions`, `leads`) hold the data
the rules need to evaluate eligibility.

## Selection size

Capped at **5** (`MAX_PROS`); targets at least **3** (`MIN_PROS`). If fewer than
3 eligible pros exist, the lead goes to all available and the shortfall is
logged (we never send to an ineligible pro or invent one). If **zero** are
eligible, the lead is held and `distribute_lead()` returns `[]`.

## Usage

```python
import database, lead_distribution as ld

conn = database.init_db(database.connect("solaryien_connect.db"))

# homeowner submits → create lead + distribute in one call
recipients = ld.submit_homeowner_project(
    conn, "lead_abc123",
    trade_category="Flooring (Tile, Hardwood, LVP)",  # normalized internally
    region_code="R5", project_title="Master bath tile", city="Orlando",
)
# -> [pro_id, pro_id, ...]  (3–5 ids)

ld.get_distribution_log(conn, "lead_abc123")   # audit trail
ld.get_pro_lead_counts(conn)                   # fairness report
```

## HTTP (optional)

```
pip install flask
python app.py            # serves on :5050

POST /api/leads                          { "trade": "...", "region": "R5", ... }
GET  /api/leads/<lead_id>/distributions  # audit log
GET  /api/pros/lead-counts               # fairness report
```

`home/post-project.html` currently has a `// Backend integration:` marker where
its form submit should `POST /api/leads`.

## Test

```
python test_distribution.py
```

Proves: region/trade/status/standing filtering, the 3–5 cap, fair rotation
(60 leads land perfectly even across the eligible pool), no duplicate sends,
full logging, and graceful under-fill / no-eligible-pro handling.
