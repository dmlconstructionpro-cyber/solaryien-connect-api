"""Bearer-token session auth. A token is issued at login and required by every
protected (dashboard/data) endpoint. Tokens are random and stored server-side,
so they can't be forged client-side and can be revoked."""
import secrets


def create_session(conn, account_type, account_id):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token, account_type, account_id) VALUES (?, ?, ?)",
                 (token, account_type, int(account_id)))
    conn.commit()
    return token


def get_session(conn, token):
    if not token:
        return None
    return conn.execute(
        "SELECT account_type, account_id FROM sessions WHERE token = ?", (token,)).fetchone()


def revoke(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def authorized(conn, token, want_type, want_id=None):
    """True if the token is valid, of the wanted type, and (if given) matches the id."""
    s = get_session(conn, token)
    if not s or s["account_type"] != want_type:
        return False
    if want_id is not None and int(s["account_id"]) != int(want_id):
        return False
    return True
