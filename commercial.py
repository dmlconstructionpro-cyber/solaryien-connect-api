"""
Solaryien Connect — Commercial platform (BuildingConnected-style).

Project Owners post commercial projects; subscribed commercial contractors find
and bid. Unlike residential (3-5 leads), a commercial project is visible to ALL
matching subscribers, and they're all notified.
"""
import json
import logging
import uuid

import csi
import regions

log = logging.getLogger("solaryien.commercial")

# Plans/coverage that include commercial access (Part 12).
def has_commercial_access(conn, pro):
    """pro is a sqlite Row from `pros`. True if their active plan covers commercial."""
    if pro is None or pro["status"] != "active":
        return False
    if (pro["coverage_type"] or "") in ("commercial", "bundled"):
        return True
    if (pro["plan"] or "") == "complete":
        return True
    # Launch Partner includes a free Standard plan -> commercial access during trial
    if conn.execute("SELECT 1 FROM launch_partner_claims WHERE pro_id = ?",
                    (pro["id"],)).fetchone():
        return True
    return False


def _mailer():
    try:
        import emailer
        return emailer.send
    except Exception:
        return lambda *a, **k: False


def _pro_trades(conn, pro_id):
    return [r["trade"] for r in conn.execute(
        "SELECT trade FROM pro_trades WHERE pro_id = ?", (pro_id,)).fetchall()]


def _pro_regions(conn, pro_id):
    return [r["region_code"] for r in conn.execute(
        "SELECT region_code FROM pro_regions WHERE pro_id = ?", (pro_id,)).fetchall()]


# ── Matching + open distribution (Part 11) ───────────────────────────────
def matching_pros(conn, region_code, division_codes):
    """All active commercial-subscribed pros in the region whose trades match."""
    out = []
    for pro in conn.execute(
            "SELECT * FROM pros WHERE status = 'active' AND work_type IN ('commercial','both')"
    ).fetchall():
        if not has_commercial_access(conn, pro):
            continue
        if region_code not in _pro_regions(conn, pro["id"]):
            continue
        if csi.pro_matches_divisions(_pro_trades(conn, pro["id"]), division_codes):
            out.append(pro)
    return out


def _distribute(conn, project, mailer=None):
    mailer = mailer or _mailer()
    divisions = json.loads(project["trades_needed"] or "[]")
    pros = matching_pros(conn, project["region_code"], divisions)
    for pro in pros:
        conn.execute(
            "INSERT INTO commercial_notifications (project_id, pro_id, kind) VALUES (?, ?, 'new_project')",
            (project["id"], pro["id"]))
        mailer(pro["email"], f"New commercial project in your area: {project['project_name']}",
               f"A new commercial project was posted in {project['project_city']}, FL: "
               f"{project['project_name']}. Bid due {str(project['bid_due_date'])[:10]}.")
    conn.commit()
    log.info("Commercial project %s distributed to %d matching pros",
             project["project_uid"], len(pros))
    return len(pros)


# ── Projects ─────────────────────────────────────────────────────────────
REQUIRED = ("project_name", "project_address", "project_city", "project_zip",
            "trades_needed", "project_description", "bid_due_date")
OPTIONAL = ("client_name", "request_type", "project_number", "project_size_sqft",
            "estimated_value", "job_walk_date", "rfis_due_date", "expected_start",
            "expected_finish", "bond_required", "insurance_minimum",
            "license_required", "trade_instructions", "note_to_bidders")


def post_project(conn, owner_id, data, distribute=True, mailer=None):
    """Create a project (region auto from ZIP) and notify all matching pros."""
    for f in REQUIRED:
        if not data.get(f):
            raise ValueError(f"{f} is required")
    trades = data["trades_needed"]
    if not isinstance(trades, str):
        trades = json.dumps(trades)
    uid = data.get("project_uid") or "P-" + uuid.uuid4().hex[:8].upper()
    region = data.get("region_code") or regions.region_for_zip(data["project_zip"])
    cols = ["project_uid", "owner_id", "region_code"] + list(REQUIRED) + list(OPTIONAL)
    vals = [uid, owner_id, region]
    for f in REQUIRED:
        vals.append(trades if f == "trades_needed" else data.get(f))
    for f in OPTIONAL:
        vals.append(data.get(f))
    conn.execute(
        f"INSERT INTO commercial_projects ({','.join(cols)}) "
        f"VALUES ({','.join('?' for _ in cols)})", vals)
    conn.commit()
    project = get_project_row(conn, uid)
    n = _distribute(conn, project, mailer) if distribute else 0
    return {"project_uid": uid, "region_code": region, "notified": n}


def get_project_row(conn, uid):
    return conn.execute(
        "SELECT * FROM commercial_projects WHERE project_uid = ?", (uid,)).fetchone()


def list_projects(conn, region=None, status="active"):
    q = "SELECT * FROM commercial_projects WHERE is_public = 1"
    args = []
    if status:
        q += " AND status = ?"; args.append(status)
    if region:
        q += " AND region_code = ?"; args.append(region)
    q += " ORDER BY bid_due_date ASC"
    return conn.execute(q, args).fetchall()


def project_detail(conn, uid, count_view=False):
    p = get_project_row(conn, uid)
    if not p:
        return None
    if count_view:
        conn.execute("UPDATE commercial_projects SET view_count = view_count + 1 WHERE project_uid = ?",
                     (uid,))
        conn.commit()
    d = dict(p)
    d["trades_needed"] = json.loads(d.get("trades_needed") or "[]")
    d["trades_display"] = [csi.label(c) for c in d["trades_needed"]]
    d["bid_count"] = conn.execute(
        "SELECT COUNT(*) c FROM commercial_bids WHERE project_id = ? AND status != 'withdrawn'",
        (p["id"],)).fetchone()["c"]
    return d


# ── Bids (Part 6 Tab 4) ──────────────────────────────────────────────────
def submit_bid(conn, uid, pro_id, bid_amount, scope_of_work,
               bid_file_name=None, bid_file_content=None):
    p = get_project_row(conn, uid)
    if not p:
        raise ValueError("unknown project")
    existing = conn.execute(
        "SELECT id, status FROM commercial_bids WHERE project_id = ? AND pro_id = ?",
        (p["id"], pro_id)).fetchone()
    if existing and existing["status"] not in ("withdrawn",):
        raise ValueError("already_bid")  # must withdraw to resubmit
    if existing:  # reactivate a withdrawn bid
        conn.execute(
            "UPDATE commercial_bids SET bid_amount=?, scope_of_work=?, bid_file_name=?, "
            "bid_file_content=?, status='submitted', submitted_at=CURRENT_TIMESTAMP WHERE id=?",
            (bid_amount, scope_of_work, bid_file_name, bid_file_content, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO commercial_bids (project_id, pro_id, bid_amount, scope_of_work, "
            "bid_file_name, bid_file_content) VALUES (?, ?, ?, ?, ?, ?)",
            (p["id"], pro_id, bid_amount, scope_of_work, bid_file_name, bid_file_content))
    conn.commit()
    owner = conn.execute("SELECT email FROM project_owner_accounts WHERE id = ?",
                         (p["owner_id"],)).fetchone()
    if owner:
        _mailer()(owner["email"], f"A new bid has been submitted for {p['project_name']}",
                  f"A contractor submitted a bid for {p['project_name']}. "
                  f"Review it in your Solaryien Connect dashboard.")
    return True


def withdraw_bid(conn, bid_id, pro_id):
    cur = conn.execute(
        "UPDATE commercial_bids SET status='withdrawn', updated_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND pro_id=? AND status='submitted'", (bid_id, pro_id))
    conn.commit()
    return cur.rowcount > 0


def list_bids(conn, project_id):
    return conn.execute(
        """SELECT b.*, p.name AS pro_name, p.company AS pro_company,
                  p.verification_status
           FROM commercial_bids b JOIN pros p ON p.id = b.pro_id
           WHERE b.project_id = ? AND b.status != 'withdrawn'
           ORDER BY b.submitted_at""", (project_id,)).fetchall()


def pro_bids(conn, pro_id):
    return conn.execute(
        """SELECT b.*, c.project_name, c.project_uid, c.project_city
           FROM commercial_bids b JOIN commercial_projects c ON c.id = b.project_id
           WHERE b.pro_id = ? ORDER BY b.submitted_at DESC""", (pro_id,)).fetchall()


def mark_win(conn, bid_id, owner_id, mailer=None):
    """Owner marks a winning bid (Part 10). Notifies the winner."""
    mailer = mailer or _mailer()
    bid = conn.execute("SELECT * FROM commercial_bids WHERE id = ?", (bid_id,)).fetchone()
    if not bid:
        return False
    proj = conn.execute("SELECT * FROM commercial_projects WHERE id = ?",
                        (bid["project_id"],)).fetchone()
    if not proj or proj["owner_id"] != owner_id:
        return False
    conn.execute("UPDATE commercial_bids SET status='won', winner_notified=1, "
                 "notified_at=CURRENT_TIMESTAMP WHERE id=?", (bid_id,))
    conn.execute("UPDATE commercial_bids SET status='lost' WHERE project_id=? AND id!=? "
                 "AND status NOT IN ('withdrawn')", (bid["project_id"], bid_id))
    conn.execute("UPDATE commercial_projects SET status='awarded', winning_bid_id=?, "
                 "awarded_at=CURRENT_TIMESTAMP, awarded_notify_sent=1 WHERE id=?",
                 (bid_id, proj["id"]))
    conn.commit()
    owner = conn.execute("SELECT first_name, last_name, company_name, email "
                         "FROM project_owner_accounts WHERE id=?", (owner_id,)).fetchone()
    pro = conn.execute("SELECT name, company, email FROM pros WHERE id=?",
                       (bid["pro_id"],)).fetchone()
    if pro and owner:
        mailer(pro["email"], f"You've been selected — {proj['project_name']}",
               f"Congratulations, {owner['company_name']} has selected your bid for "
               f"{proj['project_name']}. Please contact {owner['first_name']} "
               f"{owner['last_name']} at {owner['email']} to proceed.")
    return True


# ── Invitations (Part 8 contractor search) ───────────────────────────────
def invite_contractor(conn, uid, pro_id, owner_id, mailer=None):
    mailer = mailer or _mailer()
    p = get_project_row(conn, uid)
    if not p:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO project_invitations (project_id, pro_id, invited_by) "
        "VALUES (?, ?, ?)", (p["id"], pro_id, owner_id))
    conn.execute(
        "INSERT INTO commercial_notifications (project_id, pro_id, kind) VALUES (?, ?, 'invitation')",
        (p["id"], pro_id))
    conn.commit()
    pro = conn.execute("SELECT email FROM pros WHERE id=?", (pro_id,)).fetchone()
    if pro:
        mailer(pro["email"], f"You have been invited to bid on {p['project_name']}",
               f"You've been invited to bid on {p['project_name']} in {p['project_city']}, FL. "
               f"View it in your Solaryien Connect commercial dashboard.")
    return True


# ── Contractor's commercial project feed (Part 9) ────────────────────────
def pro_commercial_projects(conn, pro_id):
    pro_regs = set(_pro_regions(conn, pro_id))
    trades = _pro_trades(conn, pro_id)
    invited = {r["project_id"] for r in conn.execute(
        "SELECT project_id FROM project_invitations WHERE pro_id = ?", (pro_id,)).fetchall()}
    out = []
    for p in list_projects(conn):
        if p["region_code"] not in pro_regs:
            continue
        divisions = json.loads(p["trades_needed"] or "[]")
        if not csi.pro_matches_divisions(trades, divisions):
            continue
        d = dict(p)
        d["invited"] = p["id"] in invited
        d["trades_display"] = [csi.label(c) for c in divisions]
        out.append(d)
    # invited projects first, then by bid due date
    out.sort(key=lambda x: (not x["invited"], x["bid_due_date"]))
    return out


# ── Messages (Part 6 Tab 3) ──────────────────────────────────────────────
def add_message(conn, uid, sender_type, sender_id, text):
    p = get_project_row(conn, uid)
    if not p:
        return False
    conn.execute("INSERT INTO project_messages (project_id, sender_type, sender_id, message_text) "
                 "VALUES (?, ?, ?, ?)", (p["id"], sender_type, sender_id, text))
    conn.commit()
    return True


def get_messages(conn, uid, sender_type=None, sender_id=None):
    p = get_project_row(conn, uid)
    if not p:
        return []
    q = "SELECT * FROM project_messages WHERE project_id = ?"
    args = [p["id"]]
    # a pro only sees their own thread; owner sees all
    if sender_type == "pro" and sender_id is not None:
        q += " AND sender_type IN ('owner','pro') AND (sender_id = ? OR sender_type='owner')"
        args.append(sender_id)
    q += " ORDER BY sent_at"
    return conn.execute(q, args).fetchall()
