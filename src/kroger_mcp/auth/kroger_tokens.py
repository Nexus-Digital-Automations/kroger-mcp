"""Per-user Kroger OAuth token storage (encrypted at rest).

Ownership: the database-backed replacement for the single shared
``.kroger_token_user.json`` file. Each user's Kroger ``access_token`` and
``refresh_token`` are encrypted (see ``token_crypto``) and stored in the
``kroger_tokens`` table keyed by ``user_id`` — so concurrent users never
overwrite each other's auth (the multi-user correctness bug this fixes).

The reconstructed ``token_info`` dict matches the shape ``kroger-api`` persists
(``access_token``, ``token_type``, ``refresh_token``, ``expires_in``), so it can
be assigned directly to ``client.client.token_info``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from kroger_mcp.auth.token_crypto import TokenCryptoError, decrypt, encrypt

logger = logging.getLogger(__name__)


def _get_connection():
    """Get a database connection and backend name (PostgreSQL or SQLite)."""
    from kroger_mcp.analytics.database import get_backend

    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import get_pg_connection

        return get_pg_connection(), "postgresql"
    from kroger_mcp.analytics.database import get_db_connection

    return get_db_connection(), "sqlite"


def _release(conn, backend) -> None:
    """Return a connection to its home: the pool for Postgres, close for SQLite.

    A pooled psycopg connection MUST go back via ``putconn``; calling ``.close()``
    on it leaks the pool slot until the pool is exhausted. Mirrors the correct
    idiom in web/routes/auth.py.
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


def save_kroger_token(user_id: str, token_info: dict[str, Any]) -> None:
    """Encrypt and persist a user's Kroger token (insert or replace).

    Args:
        user_id: The owning user's id.
        token_info: Raw token dict from kroger-api (access_token, token_type,
            refresh_token, expires_in[, scope]).
    """
    access_enc = encrypt(token_info["access_token"])
    refresh = token_info.get("refresh_token")
    refresh_enc = encrypt(refresh) if refresh else None
    token_type = token_info.get("token_type", "bearer")
    scope = token_info.get("scope")

    now = datetime.now(timezone.utc)
    expires_in = token_info.get("expires_in")
    expires_at = now + timedelta(seconds=int(expires_in)) if expires_in else None

    conn, backend = _get_connection()
    try:
        if backend == "postgresql":
            conn.execute(
                """INSERT INTO kroger_tokens
                       (user_id, access_token, refresh_token, token_type, expires_at, scope, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET
                       access_token = EXCLUDED.access_token,
                       refresh_token = EXCLUDED.refresh_token,
                       token_type = EXCLUDED.token_type,
                       expires_at = EXCLUDED.expires_at,
                       scope = EXCLUDED.scope,
                       updated_at = EXCLUDED.updated_at""",
                (user_id, access_enc, refresh_enc, token_type, expires_at, scope, now),
            )
        else:
            conn.execute(
                """INSERT INTO kroger_tokens
                       (user_id, access_token, refresh_token, token_type, expires_at, scope, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       access_token = excluded.access_token,
                       refresh_token = excluded.refresh_token,
                       token_type = excluded.token_type,
                       expires_at = excluded.expires_at,
                       scope = excluded.scope,
                       updated_at = excluded.updated_at""",
                (
                    user_id,
                    access_enc,
                    refresh_enc,
                    token_type,
                    expires_at.isoformat() if expires_at else None,
                    scope,
                    now.isoformat(),
                ),
            )
        conn.commit()
    finally:
        _release(conn, backend)


def load_kroger_token(user_id: str) -> dict[str, Any] | None:
    """Load and decrypt a user's Kroger token, or ``None`` if absent/corrupt.

    A decryption failure (tampered ciphertext, or a key rotation that orphaned
    this row) is treated as "no usable token" — the caller forces re-auth.
    """
    conn, backend = _get_connection()
    try:
        placeholder = "%s" if backend == "postgresql" else "?"
        cur = conn.execute(
            f"""SELECT access_token, refresh_token, token_type, expires_at, scope
                FROM kroger_tokens WHERE user_id = {placeholder}""",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        _release(conn, backend)

    if not row:
        return None

    access_enc, refresh_enc, token_type, expires_at, scope = row
    try:
        token_info: dict[str, Any] = {
            "access_token": decrypt(access_enc),
            "token_type": token_type or "bearer",
        }
        if refresh_enc:
            token_info["refresh_token"] = decrypt(refresh_enc)
    except TokenCryptoError:
        logger.warning("kroger token for user=%s is undecryptable; forcing re-auth", user_id)
        return None

    if scope:
        token_info["scope"] = scope
    if expires_at is not None:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        token_info["expires_in"] = max(0, int(remaining))

    return token_info


def delete_kroger_token(user_id: str) -> None:
    """Remove a user's stored Kroger token (e.g. on disconnect)."""
    conn, backend = _get_connection()
    try:
        placeholder = "%s" if backend == "postgresql" else "?"
        conn.execute(
            f"DELETE FROM kroger_tokens WHERE user_id = {placeholder}",
            (user_id,),
        )
        conn.commit()
    finally:
        _release(conn, backend)
