"""
Solaryien Connect — Pro account lifecycle.

    signup            status='pending', not in good standing  (NOT eligible for leads)
      -> submit_verification (license where required, COI, bg-check consent)
      -> approve_and_activate (after review + subscription payment)
    active            status='active', in_good_standing=1     (ELIGIBLE for leads)
    suspend / restore toggle eligibility without deleting the account

Passwords are stored as pbkdf2-hmac-sha256 with a per-account salt (stdlib only).
"""

import hashlib
import hmac
import os
import secrets

import lead_distribution as ld  # for normalize_trade

_PBKDF2_ROUNDS = 120_000


def _hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password, salt, expected_hash):
    if not salt or not expected_hash:
        return False
    _, h = _hash_password(password, salt)
    return hmac.compare_digest(h, expected_hash)


# ── Signup ───────────────────────────────────────────────────────────────
def create_pro_account(conn, *, name, company, email, phone, password,
                       trades=(), regions=(), plan=None, coverage_type=None):
    """
    Create a PENDING pro account. Pending pros are NOT eligible to receive
    leads until they are verified and activated (approve_and_activate).
    Returns the new pro id.
    """
    salt, pwhash = _hash_password(password)
    verify_token = secrets.token_urlsafe(24)
    cur = conn.execute(
        """INSERT INTO pros (name, company, email, phone, status, in_good_standing,
                             password_hash, password_salt, plan, coverage_type,
                             email_verified, email_verify_token)
           VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, 0, ?)""",
        (name, company, email, phone, pwhash, salt, plan, coverage_type, verify_token),
    )
    pro_id = cur.lastrowid
    for t in trades:
        conn.execute("INSERT OR IGNORE INTO pro_trades (pro_id, trade) VALUES (?, ?)",
                     (pro_id, ld.normalize_trade(t)))
    for r in regions:
        conn.execute("INSERT OR IGNORE INTO pro_regions (pro_id, region_code) VALUES (?, ?)",
                     (pro_id, r))
    conn.commit()
    return pro_id


# ── Verification + activation ────────────────────────────────────────────
def approve_and_activate(conn, pro_id, *, plan=None, coverage_type=None):
    """
    Mark a pro verified, subscribed, and active — making them eligible for
    leads. Call after verification review passes and payment succeeds.
    """
    fields = ["status = 'active'", "in_good_standing = 1"]
    params = []
    if plan is not None:
        fields.append("plan = ?"); params.append(plan)
    if coverage_type is not None:
        fields.append("coverage_type = ?"); params.append(coverage_type)
    params.append(pro_id)
    conn.execute(f"UPDATE pros SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    return get_pro(conn, pro_id)


def set_standing(conn, pro_id, in_good_standing):
    """Toggle good standing (e.g., lapsed payment) without deleting the account."""
    conn.execute("UPDATE pros SET in_good_standing = ? WHERE id = ?",
                 (1 if in_good_standing else 0, pro_id))
    conn.commit()


def set_status(conn, pro_id, status):
    """Set 'active' | 'suspended' | 'inactive' | 'pending'."""
    conn.execute("UPDATE pros SET status = ? WHERE id = ?", (status, pro_id))
    conn.commit()


# ── Login ────────────────────────────────────────────────────────────────
def authenticate(conn, email, password):
    """Return the pro row on success, else None."""
    row = conn.execute("SELECT * FROM pros WHERE email = ?", (email,)).fetchone()
    if row is None:
        return None
    if verify_password(password, row["password_salt"], row["password_hash"]):
        return row
    return None


def get_pro(conn, pro_id):
    return conn.execute("SELECT * FROM pros WHERE id = ?", (pro_id,)).fetchone()


def verify_email(conn, token):
    """Mark a pro's email verified via their token. Returns True if it matched."""
    if not token:
        return False
    cur = conn.execute(
        "UPDATE pros SET email_verified = 1, email_verify_token = NULL WHERE email_verify_token = ?",
        (token,))
    conn.commit()
    return cur.rowcount > 0
