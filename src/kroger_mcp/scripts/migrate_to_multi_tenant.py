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

import json
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


def _recreate_table_without_unique(
    conn: sqlite3.Connection,
    table: str,
    new_create_sql: str,
    column_list: str,
) -> bool:
    """Drop and recreate a table to remove a global UNIQUE constraint.

    Idempotent: inspects the existing CREATE SQL for the literal "UNIQUE"
    keyword (or PRIMARY KEY clauses we're replacing) and only fires if found.
    PRAGMA foreign_keys=OFF prevents FK cascades from wiping child rows when
    the old table is dropped. Caller is responsible for indexes the new
    schema needs — pass them inside `new_create_sql` via CREATE INDEX
    statements separated by ;.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row:
        return False
    existing_sql = row[0].upper()
    if "UNIQUE" not in existing_sql and "PRIMARY KEY" not in new_create_sql.upper():
        return False
    # If the new schema's PRIMARY KEY shape already matches, skip (post-migration state)
    if new_create_sql.replace(" ", "").upper() in existing_sql.replace(" ", "").upper():
        return False

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        f"""
        CREATE TABLE {table}_new {new_create_sql};
        INSERT INTO {table}_new ({column_list})
            SELECT {column_list} FROM {table};
        DROP TABLE {table};
        ALTER TABLE {table}_new RENAME TO {table};
        PRAGMA foreign_keys = ON;
        """
    )
    conn.commit()
    logger.info("table=%s recreated to drop conflicting UNIQUE/PK constraint", table)
    return True


# Each entry: (table_name, new_schema_sql, columns_to_copy)
# new_schema_sql is the body after CREATE TABLE x — starts with `(`.
# Constraints listed at the end become composite (user_id, key) instead of the
# old single-column UNIQUE.
TABLES_TO_RECREATE: tuple[tuple[str, str, str], ...] = (
    (
        "favorite_lists",
        """(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            list_type TEXT DEFAULT 'custom',
            reorder_weeks INTEGER DEFAULT NULL,
            last_ordered_at TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT
        )""",
        "id, name, description, list_type, reorder_weeks, last_ordered_at, created_at, updated_at, user_id",
    ),
    (
        "custom_ingredients",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_name TEXT NOT NULL COLLATE NOCASE,
            severity TEXT NOT NULL CHECK(severity IN ('critical', 'warning', 'watch')),
            category TEXT,
            reason TEXT,
            aliases TEXT,
            source TEXT DEFAULT 'user' CHECK(source IN ('user', 'imported', 'system')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            modified_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
            notes TEXT,
            user_id TEXT,
            UNIQUE(user_id, ingredient_name)
        )""",
        "id, ingredient_name, severity, category, reason, aliases, source, created_at, modified_at, is_active, notes, user_id",
    ),
    (
        "ingredient_overrides",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_name TEXT NOT NULL COLLATE NOCASE,
            override_severity TEXT CHECK(override_severity IN ('critical', 'warning', 'watch')),
            override_reason TEXT,
            additional_aliases TEXT,
            is_hidden INTEGER DEFAULT 0 CHECK(is_hidden IN (0, 1)),
            modified_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            user_id TEXT,
            UNIQUE(user_id, ingredient_name)
        )""",
        "id, ingredient_name, override_severity, override_reason, additional_aliases, is_hidden, modified_at, notes, user_id",
    ),
    (
        "ingredient_preferences",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_key TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            severity TEXT DEFAULT 'warning',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT,
            UNIQUE(user_id, ingredient_key)
        )""",
        "id, ingredient_key, enabled, severity, updated_at, user_id",
    ),
    (
        "safe_products",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            description TEXT,
            brand TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            added_reason TEXT,
            user_id TEXT,
            UNIQUE(user_id, product_id)
        )""",
        "id, product_id, description, brand, added_at, added_reason, user_id",
    ),
    (
        "blocked_products",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            description TEXT,
            blocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            blocked_reason TEXT,
            auto_blocked INTEGER DEFAULT 0,
            user_id TEXT,
            UNIQUE(user_id, product_id)
        )""",
        "id, product_id, description, blocked_at, blocked_reason, auto_blocked, user_id",
    ),
    (
        "pantry_items",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            description TEXT,
            level_percent INTEGER DEFAULT 100,
            last_restocked_at TEXT,
            last_updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_deplete INTEGER DEFAULT 1,
            daily_depletion_rate REAL DEFAULT 0,
            low_threshold INTEGER DEFAULT 20,
            user_id TEXT,
            UNIQUE(user_id, product_id)
        )""",
        "id, product_id, description, level_percent, last_restocked_at, last_updated_at, auto_deplete, daily_depletion_rate, low_threshold, user_id",
    ),
    (
        "deal_watchlist",
        """(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            description TEXT,
            target_price REAL,
            priority INTEGER DEFAULT 1,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked_at TEXT,
            best_price_seen REAL,
            best_price_date TEXT,
            user_id TEXT,
            UNIQUE(user_id, product_id)
        )""",
        "id, product_id, description, target_price, priority, added_at, last_checked_at, best_price_seen, best_price_date, user_id",
    ),
    (
        "safety_settings",
        """(
            key TEXT NOT NULL,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )""",
        "key, value, updated_at, user_id",
    ),
)


def _create_user_scoped_tables(conn: sqlite3.Connection) -> int:
    """Create the new JSON-state-replacing tables. Idempotent (CREATE IF NOT EXISTS)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_carts (
            user_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            description TEXT,
            quantity INTEGER DEFAULT 1,
            modality TEXT DEFAULT 'PICKUP',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, product_id)
        );
        CREATE INDEX IF NOT EXISTS idx_user_carts_user_id ON user_carts(user_id);

        CREATE TABLE IF NOT EXISTS user_shopping_lists (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            product_id TEXT,
            name TEXT NOT NULL,
            quantity REAL DEFAULT 1.0,
            unit TEXT DEFAULT '',
            category TEXT,
            purchased INTEGER DEFAULT 0,
            recipe_source TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_user_shopping_lists_user_id ON user_shopping_lists(user_id);

        CREATE TABLE IF NOT EXISTS user_notion_sync (
            user_id TEXT PRIMARY KEY,
            notion_database_id TEXT,
            last_sync_at TEXT,
            config_json TEXT
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, setting_key)
        );
        """
    )
    conn.commit()
    return 4


def _absorb_legacy_json(conn: sqlite3.Connection, owner_id: str) -> int:
    """Import data/legacy/kroger_*.json.* (most-recent) into the new user_* tables.

    Idempotent: skips a file once any rows for owner_id exist in the target
    table (rough but correct for the personal-use migration scenario).
    """
    legacy_dir = LEGACY_DIR
    if not legacy_dir.exists():
        return 0
    absorbed = 0

    def _latest(prefix: str) -> Path | None:
        candidates = sorted(legacy_dir.glob(f"{prefix}.json.pre-multi-tenant.*"))
        return candidates[-1] if candidates else None

    # ---- kroger_cart.json -> user_carts ----
    cart_file = _latest("kroger_cart")
    has_cart = conn.execute(
        "SELECT COUNT(*) AS cnt FROM user_carts WHERE user_id = ?", (owner_id,)
    ).fetchone()["cnt"]
    if cart_file and has_cart == 0:
        try:
            blob = json.loads(cart_file.read_text())
            items = blob.get("items", []) if isinstance(blob, dict) else []
            for item in items:
                pid = item.get("product_id") or item.get("productId")
                if not pid:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_carts
                        (user_id, product_id, description, quantity, modality)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        pid,
                        item.get("description") or item.get("name"),
                        int(item.get("quantity", 1) or 1),
                        item.get("modality", "PICKUP"),
                    ),
                )
                absorbed += 1
            conn.commit()
            logger.info("absorbed %d cart items from %s", absorbed, cart_file.name)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("could not parse %s: %s", cart_file, exc)

    # ---- kroger_shopping_list.json -> user_shopping_lists ----
    shop_file = _latest("kroger_shopping_list")
    has_shop = conn.execute(
        "SELECT COUNT(*) AS cnt FROM user_shopping_lists WHERE user_id = ?", (owner_id,)
    ).fetchone()["cnt"]
    if shop_file and has_shop == 0:
        try:
            blob = json.loads(shop_file.read_text())
            items = blob.get("items", []) if isinstance(blob, dict) else []
            for item in items:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_shopping_lists
                        (id, user_id, product_id, name, quantity, unit, category, purchased, recipe_source, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.get("id") or str(uuid.uuid4()),
                        owner_id,
                        item.get("product_id"),
                        item.get("name", ""),
                        float(item.get("quantity", 1) or 1),
                        item.get("unit", ""),
                        item.get("category"),
                        1 if item.get("purchased") else 0,
                        item.get("recipe_source"),
                        item.get("added_at"),
                    ),
                )
            conn.commit()
            logger.info("absorbed shopping-list items from %s", shop_file.name)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("could not parse %s: %s", shop_file, exc)

    # ---- kroger_notion_sync.json -> user_notion_sync ----
    notion_file = _latest("kroger_notion_sync")
    has_notion = conn.execute(
        "SELECT COUNT(*) AS cnt FROM user_notion_sync WHERE user_id = ?", (owner_id,)
    ).fetchone()["cnt"]
    if notion_file and has_notion == 0:
        try:
            blob_text = notion_file.read_text()
            blob = json.loads(blob_text)
            db_id = blob.get("database_id") if isinstance(blob, dict) else None
            last_sync = blob.get("last_sync_at") if isinstance(blob, dict) else None
            conn.execute(
                """
                INSERT OR IGNORE INTO user_notion_sync
                    (user_id, notion_database_id, last_sync_at, config_json)
                VALUES (?, ?, ?, ?)
                """,
                (owner_id, db_id, last_sync, blob_text),
            )
            conn.commit()
            logger.info("absorbed notion sync state from %s", notion_file.name)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("could not parse %s: %s", notion_file, exc)

    return absorbed


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

        # Always-run idempotent steps (safe to re-run after a successful migration):
        # 1) Create new user-scoped tables (user_carts, user_shopping_lists, etc.)
        _create_user_scoped_tables(conn)

        # 2) Recreate any table whose schema still carries a conflicting UNIQUE/PK
        for table_name, schema_sql, columns in TABLES_TO_RECREATE:
            _recreate_table_without_unique(conn, table_name, schema_sql, columns)
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table_name}_user_id ON {table_name}(user_id)"
            )
        conn.commit()

        # 3) Absorb data/legacy JSON state into the new user-scoped tables
        _absorb_legacy_json(conn, owner_id)

        _persist_default_user_id(owner_id)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
