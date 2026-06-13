"""Project Owner accounts (GCs, developers, property owners) — free, no verification."""
import accounts  # reuse password hashing


def create_owner(conn, *, first_name, last_name, company_name, email, phone,
                 password, title=None):
    salt, pwhash = accounts._hash_password(password)
    cur = conn.execute(
        """INSERT INTO project_owner_accounts
               (first_name, last_name, company_name, title, email, phone,
                password_hash, password_salt, email_verified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (first_name, last_name, company_name, title, email, phone, pwhash, salt))
    conn.commit()
    return cur.lastrowid


def authenticate(conn, email, password):
    row = conn.execute(
        "SELECT * FROM project_owner_accounts WHERE email = ?", (email,)).fetchone()
    if row and accounts.verify_password(password, row["password_salt"], row["password_hash"]):
        return row
    return None


def get_owner(conn, owner_id):
    return conn.execute(
        "SELECT * FROM project_owner_accounts WHERE id = ?", (owner_id,)).fetchone()
