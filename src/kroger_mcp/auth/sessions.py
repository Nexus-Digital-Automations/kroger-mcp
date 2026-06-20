"""Session management — create, validate, and delete user sessions.

Sessions are stored in the database (PostgreSQL or SQLite) and validated
via a token stored in an HTTP-only cookie.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from kroger_mcp.cache import get_redis

logger = logging.getLogger(__name__)

SESSION_EXPIRY_DAYS = 30
# TTL for cached session-validation results. Bounds staleness far below the
# 30-day session lifetime so a deactivated/changed user is re-read from the DB
# within this window even if no explicit delete fires.
_SESSION_CACHE_TTL = 300


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


def _release(conn, backend) -> None:
    """Return a connection to its home: the pool for Postgres, close for SQLite.

    A pooled psycopg connection MUST go back via ``putconn``. Calling ``.close()``
    on it instead (which the SQLite path needs) drops the socket without returning
    the pool slot, so each uncached validation permanently leaks one connection
    until the pool is exhausted and every request fails with PoolTimeout. Mirrors
    the correct idiom in web/routes/auth.py.
    """
    if backend == "postgresql":
        from kroger_mcp.analytics.pg_database import _get_pool

        try:
            conn.rollback()
        except Exception:
            pass
        _get_pool().putconn(conn)
    else:
        conn.close()


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
        _release(conn, backend)

    return token


def validate_session(token: str) -> dict | None:
    """Validate a session token. Returns user dict if valid, None if expired/invalid."""
    token_hash = _hash_token(token)
    cache_key = f"sess:{token_hash}"

    # Read-through cache. Keyed on the SHA-256 hash, never the raw token.
    # Best-effort: any Redis error falls through to the DB path.
    redis_client = get_redis()
    if redis_client is not None:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                user_dict: dict = json.loads(cached)
                return user_dict
        except Exception as exc:
            logger.warning("redis session cache read failed (%s); using DB", exc)

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

        result = {
            "id": str(user_id),
            "email": email,
            "display_name": display_name,
            "kroger_profile_id": kroger_profile_id,
        }

        # Populate the read-through cache only on a successful validation
        # (never cache a miss). Best-effort: a Redis failure must not affect
        # the request. NOTE: if a user is later deactivated (is_active=0)
        # without an explicit delete_session, the _SESSION_CACHE_TTL (300s)
        # caps how long this stale entry can be served.
        if redis_client is not None:
            try:
                # set-with-expiry (ex=) — atomic, equivalent to SETEX but the
                # non-deprecated form in redis-py 8.x.
                redis_client.set(
                    cache_key, json.dumps(result), ex=_SESSION_CACHE_TTL
                )
            except Exception as exc:
                logger.warning("redis session cache write failed (%s)", exc)

        return result
    finally:
        _release(conn, backend)


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
        _release(conn, backend)

    # Evict the read-through cache so a logged-out / expired session is not
    # served from a stale entry. Best-effort; never raises. This path covers
    # both explicit logout and the expiry-triggered delete in validate_session.
    redis_client = get_redis()
    if redis_client is not None:
        try:
            redis_client.delete(f"sess:{token_hash}")
        except Exception as exc:
            logger.warning("redis session cache delete failed (%s)", exc)


def _dict_factory(cursor, row):
    """SQLite row factory that returns dicts."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
