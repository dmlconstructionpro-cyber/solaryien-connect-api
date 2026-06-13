"""
Solaryien Connect — database schema and connection helpers.

Pure standard-library (sqlite3). No third-party dependencies, so the lead
distribution logic can be imported and tested without Flask installed.

Tables relevant to lead distribution
------------------------------------
pros               Verified contractor accounts (status + good standing).
pro_trades         Trades each pro offers (many-to-many).
pro_regions        Florida regions each pro is subscribed to (many-to-many).
leads              Homeowner / commercial projects submitted for distribution.
lead_distributions Which pro received which lead, and when  (REQUIRED).
pro_lead_count     Running lead totals per pro, for fair rotation (REQUIRED).
"""

import sqlite3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- ── Verified contractor accounts ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pros (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    company           TEXT,
    email             TEXT UNIQUE,
    phone             TEXT,
    -- new signups start 'pending'; become 'active' after verify + subscribe.
    -- only 'active' pros are eligible. 'suspended' / 'inactive' are not.
    status            TEXT NOT NULL DEFAULT 'pending',
    -- 1 = in good standing (paid, verified, no violations); 0 = not
    in_good_standing  INTEGER NOT NULL DEFAULT 0,
    -- credentials (pbkdf2-hmac-sha256); null until an account sets a password
    password_hash     TEXT,
    password_salt     TEXT,
    plan              TEXT,                       -- standard | pro | complete
    coverage_type     TEXT,                       -- residential | commercial | bundled
    work_type         TEXT NOT NULL DEFAULT 'residential',  -- residential | commercial | both
    -- business profile (step 2)
    years_in_business INTEGER,
    website           TEXT,
    bio               TEXT,
    -- verification (step 3 -> admin review)
    -- incomplete | pending_verification | approved | rejected
    verification_status TEXT NOT NULL DEFAULT 'incomplete',
    rejection_reason  TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Trades a pro offers (must match the homeowner's requested trade) ──────
CREATE TABLE IF NOT EXISTS pro_trades (
    pro_id  INTEGER NOT NULL REFERENCES pros(id) ON DELETE CASCADE,
    trade   TEXT NOT NULL,
    PRIMARY KEY (pro_id, trade)
);

-- ── Regions a pro is subscribed to (must contain the project's region) ────
CREATE TABLE IF NOT EXISTS pro_regions (
    pro_id       INTEGER NOT NULL REFERENCES pros(id) ON DELETE CASCADE,
    region_code  TEXT NOT NULL,          -- R1..R7
    PRIMARY KEY (pro_id, region_code)
);

-- ── Homeowner / commercial projects submitted for matching ───────────────
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT UNIQUE NOT NULL,        -- public UUID
    lead_type       TEXT NOT NULL DEFAULT 'residential',
    project_title   TEXT,
    trade_category  TEXT NOT NULL,               -- canonical trade
    region_code     TEXT NOT NULL,               -- R1..R7
    city            TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── REQUIRED: which pro received which lead, and when ─────────────────────
CREATE TABLE IF NOT EXISTS lead_distributions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    pro_id          INTEGER NOT NULL REFERENCES pros(id) ON DELETE CASCADE,
    distributed_at  DATETIME NOT NULL,
    status          TEXT NOT NULL DEFAULT 'sent',  -- sent | accepted | declined
    decline_reason  TEXT,
    responded_at    DATETIME,
    -- guarantees the same lead is never sent to the same pro twice
    UNIQUE (lead_id, pro_id)
);
CREATE INDEX IF NOT EXISTS idx_ld_lead   ON lead_distributions(lead_id);
CREATE INDEX IF NOT EXISTS idx_ld_pro    ON lead_distributions(pro_id);
CREATE INDEX IF NOT EXISTS idx_ld_when   ON lead_distributions(distributed_at);

-- ── REQUIRED: running lead totals per pro, for fair rotation ──────────────
CREATE TABLE IF NOT EXISTS pro_lead_count (
    pro_id        INTEGER PRIMARY KEY REFERENCES pros(id) ON DELETE CASCADE,
    total_leads   INTEGER NOT NULL DEFAULT 0,    -- lifetime leads received
    last_lead_at  DATETIME,                      -- most recent distribution
    updated_at    DATETIME
);

-- ── Pro documents (COI, license) stored as BLOBs (not web-accessible) ────
CREATE TABLE IF NOT EXISTS pro_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pro_id       INTEGER NOT NULL REFERENCES pros(id) ON DELETE CASCADE,
    doc_type     TEXT NOT NULL,                  -- coi | license
    filename     TEXT,
    mime         TEXT,
    content      BLOB,
    uploaded_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (pro_id, doc_type)
);

-- ── Digitally signed agreements (background check auth, verification, ToS) ─
CREATE TABLE IF NOT EXISTS pro_agreements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pro_id          INTEGER NOT NULL REFERENCES pros(id) ON DELETE CASCADE,
    -- background_check_authorization | contractor_verification_agreement | terms_of_service
    agreement_type  TEXT NOT NULL,
    signed_name     TEXT NOT NULL,
    signed_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    ip              TEXT,
    UNIQUE (pro_id, agreement_type)
);

-- ═══ COMMERCIAL PLATFORM ═════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS project_owner_accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    company_name   TEXT NOT NULL,
    title          TEXT,
    email          TEXT UNIQUE NOT NULL,
    phone          TEXT,
    password_hash  TEXT NOT NULL,
    password_salt  TEXT,
    email_verified INTEGER DEFAULT 0,
    is_active      INTEGER DEFAULT 1,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS commercial_projects (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_uid         TEXT UNIQUE NOT NULL,
    owner_id            INTEGER REFERENCES project_owner_accounts(id),
    project_name        TEXT NOT NULL,
    project_address     TEXT NOT NULL,
    project_city        TEXT NOT NULL,
    project_state       TEXT DEFAULT 'FL',
    project_zip         TEXT NOT NULL,
    region_code         TEXT,                       -- R1-R7 auto from ZIP
    trades_needed       TEXT NOT NULL,              -- JSON array of CSI divisions
    project_description TEXT NOT NULL,
    bid_due_date        DATETIME NOT NULL,
    client_name         TEXT,
    request_type        TEXT DEFAULT 'Bid',         -- Bid | Budget | RFI
    project_number      TEXT,
    project_size_sqft   INTEGER,
    estimated_value     TEXT,
    job_walk_date       DATETIME,
    rfis_due_date       DATETIME,
    expected_start      DATETIME,
    expected_finish     DATETIME,
    bond_required       INTEGER DEFAULT 0,
    insurance_minimum   TEXT,
    license_required    INTEGER DEFAULT 0,
    trade_instructions  TEXT,
    note_to_bidders     TEXT,
    status              TEXT DEFAULT 'active',      -- active | closed | awarded | cancelled
    winning_bid_id      INTEGER,
    awarded_at          DATETIME,
    awarded_notify_sent INTEGER DEFAULT 0,
    is_public           INTEGER DEFAULT 1,
    view_count          INTEGER DEFAULT 0,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_comm_projects_status ON commercial_projects(status);
CREATE INDEX IF NOT EXISTS idx_comm_projects_region ON commercial_projects(region_code);
CREATE INDEX IF NOT EXISTS idx_comm_projects_bid_due ON commercial_projects(bid_due_date);
CREATE INDEX IF NOT EXISTS idx_comm_projects_owner ON commercial_projects(owner_id);

CREATE TABLE IF NOT EXISTS project_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES commercial_projects(id) ON DELETE CASCADE,
    file_name   TEXT NOT NULL,
    mime        TEXT,
    file_type   TEXT,                              -- plans | specs | addendum | other
    file_size   INTEGER,
    content     BLOB,
    uploaded_by INTEGER,                            -- project_owner_accounts.id
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_invitations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER REFERENCES commercial_projects(id) ON DELETE CASCADE,
    pro_id        INTEGER REFERENCES pros(id),
    invited_by    INTEGER REFERENCES project_owner_accounts(id),
    status        TEXT DEFAULT 'pending',           -- pending | viewed | bidding | declined | no_response
    sent_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    viewed_at     DATETIME,
    responded_at  DATETIME,
    decline_reason TEXT,
    UNIQUE (project_id, pro_id)
);

CREATE TABLE IF NOT EXISTS commercial_bids (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES commercial_projects(id) ON DELETE CASCADE,
    pro_id          INTEGER REFERENCES pros(id),
    bid_amount      REAL NOT NULL,
    scope_of_work   TEXT NOT NULL,
    bid_file_name   TEXT,
    bid_file_content BLOB,
    status          TEXT DEFAULT 'submitted',        -- submitted | under_review | won | lost | withdrawn
    winner_notified INTEGER DEFAULT 0,
    notified_at     DATETIME,
    submitted_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, pro_id)
);
CREATE INDEX IF NOT EXISTS idx_bids_project ON commercial_bids(project_id);
CREATE INDEX IF NOT EXISTS idx_bids_pro ON commercial_bids(pro_id);
CREATE INDEX IF NOT EXISTS idx_bids_status ON commercial_bids(status);

CREATE TABLE IF NOT EXISTS project_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER REFERENCES commercial_projects(id) ON DELETE CASCADE,
    sender_type  TEXT NOT NULL,                     -- owner | pro
    sender_id    INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    is_read      INTEGER DEFAULT 0,
    sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS commercial_notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER REFERENCES commercial_projects(id) ON DELETE CASCADE,
    pro_id        INTEGER REFERENCES pros(id),
    kind          TEXT NOT NULL,                    -- new_project | invitation | won
    notified_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Launch Partner: single-row seat counter (starts at 5000) ─────────────
CREATE TABLE IF NOT EXISTS launch_partner (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    remaining   INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    updated_at  DATETIME
);

-- ── Launch Partner claims (one row per pro who claimed a seat) ───────────
CREATE TABLE IF NOT EXISTS launch_partner_claims (
    pro_id            INTEGER PRIMARY KEY REFERENCES pros(id) ON DELETE CASCADE,
    apex_tier         TEXT,                        -- starter | professional | enterprise
    claimed_at        DATETIME NOT NULL,
    trial_end         DATETIME NOT NULL,           -- claimed_at + 3 months
    connect_plan      TEXT DEFAULT 'standard-3region-free3mo',
    reminder_14_sent  INTEGER NOT NULL DEFAULT 0,
    reminder_7_sent   INTEGER NOT NULL DEFAULT 0,
    converted         INTEGER NOT NULL DEFAULT 0,
    cancelled         INTEGER NOT NULL DEFAULT 0
);
"""

# Launch Partner starts with this many seats.
LAUNCH_PARTNER_SEATS = 5000


def connect(path=":memory:"):
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    """Create all tables/indexes if they do not already exist."""
    conn.executescript(SCHEMA_SQL)
    # Seed the Launch Partner counter exactly once.
    conn.execute(
        "INSERT OR IGNORE INTO launch_partner (id, remaining, total, updated_at) "
        "VALUES (1, ?, ?, CURRENT_TIMESTAMP)",
        (LAUNCH_PARTNER_SEATS, LAUNCH_PARTNER_SEATS))
    conn.commit()
    return conn
