"""
Migrate the single-tenant Kroger MCP analytics DB to multi-tenant.

Owns: schema evolution + JSON-state relocation for ADR `specs/multi-tenant-user-scoping.md`.
Runs as `uv run python -m kroger_mcp.scripts.migrate_to_multi_tenant`.

Idempotent — re-running after success exits 0 with "already migrated".

State diagram (per table):
    [no user_id column] --add column + backfill--> [user_id NULL allowed, all rows = owner]
    [user_id NULL allowed]                       --(future) NOT NULL constraint via recreate

Failure modes:
    - Missing JEREMY_EMAIL / JEREMY_PASSWORD env: exits 2.
    - DB backup write fails: exits 3, no schema changes attempted.
    - Schema migration partial-fail: backup is intact; restore manually.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from kroger_mcp.analytics.database import get_db_connection, get_db_path
from kroger_mcp.auth.passwords import hash_password

logger = logging.getLogger("kroger_mcp.migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Tables that hold per-user state. New writes from authenticated routes must
# carry user_id; reads must filter by it. Tables not listed here (products,
# price_history, whole_foods_catalog, seasonal_patterns, deal_scan_results,
# users, user_sessions) remain global / shared.
USER_SCOPED_TABLES = (
    "recipes",
    "recipe_ingredients",
    "favorite_lists",
    "favorite_list_items",
    "meal_plans",
    "meal_entries",
    "meal_log",
    "meal_log_items",
    "pantry_items",
    "pantry_consumption_log",
    "safe_products",
    "blocked_products",
    "ingredient_preferences",
    "safety_settings",
    "custom_ingredients",
    "ingredient_overrides",
    "deal_watchlist",
    "orders",
    "purchase_events",
    "product_statistics",
)

JSON_FILES_TO_RELOCATE = (
    "kroger_recipes.json",
    "kroger_cart.json",
    "kroger_shopping_list.json",
    "kroger_order_history.json",
    "kroger_notion_sync.json",
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEGACY_DIR = PROJECT_ROOT / "data" / "legacy"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
ENV_FILE = PROJECT_ROOT / ".env"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _ensure_owner(conn: sqlite3.Connection, email: str, password: str) -> str:
    """Create the owner account if missing; return its user_id."""
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        logger.info("owner already exists email=%s id=%s", email, row[0])
        return str(row[0])

    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
        (user_id, email, hash_password(password), "jeremyparker"),
    )
    conn.commit()
    logger.info("owner created email=%s id=%s", email, user_id)
    return user_id


def _backup_db() -> Path:
    src = Path(get_db_path())
    if not src.exists():
        raise FileNotFoundError(f"analytics DB missing at {src}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{src.name}.pre-multi-tenant.{ts}"
    shutil.copy2(src, dest)
    logger.info("db backup written to %s", dest)
    return dest


def _add_user_id_column(conn: sqlite3.Connection, table: str, owner_id: str) -> int:
    """Add user_id column to `table` if missing, backfill, index. Returns rows backfilled."""
    if not _table_exists(conn, table):
        logger.info("skip table=%s (does not exist in this DB)", table)
        return 0
    if _column_exists(conn, table, "user_id"):
        logger.info("skip table=%s (user_id already present)", table)
        return 0
    conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
    cur = conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner_id,))
    backfilled = cur.rowcount
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user_id ON {table}(user_id)")
    logger.info("table=%s user_id added, %d rows backfilled to owner", table, backfilled)
    return backfilled


def _relocate_json_files() -> list[Path]:
    """Move root-level state JSON into data/legacy/ with a timestamp suffix."""
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for fname in JSON_FILES_TO_RELOCATE:
        src = PROJECT_ROOT / fname
        if not src.exists():
            continue
        dest = LEGACY_DIR / f"{fname}.pre-multi-tenant.{ts}"
        shutil.move(str(src), str(dest))
        moved.append(dest)
        logger.info("relocated %s -> %s", fname, dest)
    return moved


def _persist_default_user_id(owner_id: str) -> None:
    """Append KROGER_MCP_DEFAULT_USER_ID=<id> to .env so MCP/scripts can resolve it."""
    key = "KROGER_MCP_DEFAULT_USER_ID"
    line = f"{key}={owner_id}\n"
    if ENV_FILE.exists():
        existing = ENV_FILE.read_text()
        if key in existing:
            logger.info("%s already set in .env — leaving as-is", key)
            return
        ENV_FILE.write_text(existing.rstrip("\n") + "\n" + line)
    else:
        ENV_FILE.write_text(line)
    logger.info("wrote %s to .env", key)


def _is_already_migrated(conn: sqlite3.Connection) -> bool:
    """A migrated DB has user_id on every table that exists in USER_SCOPED_TABLES."""
    for t in USER_SCOPED_TABLES:
        if _table_exists(conn, t) and not _column_exists(conn, t, "user_id"):
            return False
    return True


def _drop_global_unique_on_favorite_lists(conn: sqlite3.Connection) -> bool:
    """Recreate favorite_lists without the global UNIQUE constraint on `name`.

    Two users must be allowed to both name a list "My Favorites". SQLite can't
    drop a column-level constraint in place, so we recreate the table.
    Returns True if recreation happened, False if already done.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='favorite_lists'"
    ).fetchone()
    if not row or "UNIQUE" not in row[0].upper():
        return False

    # PRAGMA foreign_keys=OFF prevents the FK cascade from wiping
    # favorite_list_items when we DROP the old table. ON_FK is restored after.
    # Must be set on the connection (not inside the script) because PRAGMA
    # foreign_keys is a no-op inside an active transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE favorite_lists_new (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            list_type TEXT DEFAULT 'custom',
            reorder_weeks INTEGER DEFAULT NULL,
            last_ordered_at TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT
        );
        INSERT INTO favorite_lists_new
            (id, name, description, list_type, reorder_weeks,
             last_ordered_at, created_at, updated_at, user_id)
            SELECT id, name, description, list_type, reorder_weeks,
                   last_ordered_at, created_at, updated_at, user_id
            FROM favorite_lists;
        DROP TABLE favorite_lists;
        ALTER TABLE favorite_lists_new RENAME TO favorite_lists;
        CREATE INDEX IF NOT EXISTS idx_favorite_lists_user_id ON favorite_lists(user_id);
        PRAGMA foreign_keys = ON;
        """
    )
    conn.commit()
    logger.info("favorite_lists recreated without global UNIQUE on name")
    return True


def main() -> int:
    email = os.environ.get("JEREMY_EMAIL")
    password = os.environ.get("JEREMY_PASSWORD")
    if not email or not password:
        logger.error("JEREMY_EMAIL and JEREMY_PASSWORD must be set in env")
        return 2

    conn = get_db_connection()
    try:
        already = _is_already_migrated(conn)
        if not already:
            _backup_db()

        owner_id = _ensure_owner(conn, email, password)

        if not already:
            total = 0
            for t in USER_SCOPED_TABLES:
                total += _add_user_id_column(conn, t, owner_id)
            conn.commit()
            logger.info(
                "schema migration complete, %d rows backfilled across %d tables",
                total,
                len(USER_SCOPED_TABLES),
            )
            moved = _relocate_json_files()
            logger.info("relocated %d JSON state files", len(moved))
        else:
            logger.info("already migrated — no schema changes needed")

        # Idempotent: only fires if the legacy UNIQUE constraint is still present
        _drop_global_unique_on_favorite_lists(conn)

        _persist_default_user_id(owner_id)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
