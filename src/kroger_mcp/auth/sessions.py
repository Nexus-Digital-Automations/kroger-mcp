"""Session management — create, validate, and delete user sessions.

Sessions are stored in the database (PostgreSQL or SQLite) and validated
via a token stored in an HTTP-only cookie.
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone

SESSION_EXPIRY_DAYS = 30


def _generate_token() -> str:
    """Generate a cryptographically random session token."""
    return os.urandom(32).hex()


def _hash_token(token: str) -> str:
    """Hash a session token for storage (SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_connection():
    """Get a database connection (PostgreSQL or SQLite)."""
    from kroger_mcp.analytics.database import get_backend

    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import get_pg_connection

        return get_pg_connection(), "postgresql"
    else:
        from kroger_mcp.analytics.database import get_db_connection

        return get_db_connection(), "sqlite"


def create_session(user_id: str, ip_address: str = "") -> str:
    """Create a new session for a user. Returns the raw token (store in cookie)."""
    token = _generate_token()
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_EXPIRY_DAYS)

    conn, backend = _get_connection()
    try:
        if backend == "postgresql":
            conn.execute(
                """INSERT INTO user_sessions (user_id, token_hash, created_at, expires_at, ip_address)
                   VALUES (%s, %s, %s, %s, %s)""",
                (user_id, token_hash, now, expires, ip_address),
            )
        else:
            conn.execute(
                """INSERT INTO user_sessions (user_id, token_hash, created_at, expires_at, ip_address)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, token_hash, now.isoformat(), expires.isoformat(), ip_address),
            )
        conn.commit()
    finally:
        conn.close()

    return token


def validate_session(token: str) -> dict | None:
    """Validate a session token. Returns user dict if valid, None if expired/invalid."""
    token_hash = _hash_token(token)

    conn, backend = _get_connection()
    try:
        if backend == "postgresql":
            cur = conn.execute(
                """SELECT u.id, u.email, u.display_name, u.kroger_profile_id, s.expires_at
                   FROM user_sessions s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = %s AND u.is_active = TRUE""",
                (token_hash,),
            )
        else:
            conn.row_factory = _dict_factory
            cur = conn.execute(
                """SELECT u.id, u.email, u.display_name, u.kroger_profile_id, s.expires_at
                   FROM user_sessions s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND u.is_active = 1""",
                (token_hash,),
            )

        row = cur.fetchone()
        if not row:
            return None

        if backend == "postgresql":
            user_id, email, display_name, kroger_profile_id, expires_at = row
        else:
            user_id = row["id"]
            email = row["email"]
            display_name = row["display_name"]
            kroger_profile_id = row["kroger_profile_id"]
            expires_at = row["expires_at"]

        # Check expiry
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            delete_session(token)
            return None

        return {
            "id": str(user_id),
            "email": email,
            "display_name": display_name,
            "kroger_profile_id": kroger_profile_id,
        }
    finally:
        conn.close()


def delete_session(token: str) -> None:
    """Delete a session by its raw token."""
    token_hash = _hash_token(token)

    conn, backend = _get_connection()
    try:
        placeholder = "%s" if backend == "postgresql" else "?"
        conn.execute(
            f"DELETE FROM user_sessions WHERE token_hash = {placeholder}",
            (token_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def _dict_factory(cursor, row):
    """SQLite row factory that returns dicts."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
