"""
Database connection and schema management for purchase analytics.

Supports SQLite (default, for dev/MCP) and PostgreSQL (for multi-user production).
Set DATABASE_URL environment variable to use PostgreSQL.
"""

import asyncio
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def get_backend() -> str:
    """Return 'postgresql' if DATABASE_URL is set, else 'sqlite'."""
    return "postgresql" if os.environ.get("DATABASE_URL") else "sqlite"


# ---------------------------------------------------------------------------
# Backend-aware connection shim
#
# The analytics/tools/web layer was written against SQLite's DB-API: `?`
# placeholders, `sqlite3.Row` (index AND name access), `conn.execute(...)`, and
# `conn.close()`. To run that same code on PostgreSQL without rewriting ~40
# files, get_db_connection() returns a thin adapter over a pooled psycopg
# connection that:
#   - translates `?` -> `%s` (escaping literal `%` -> `%%` for psycopg),
#   - translates `INSERT OR IGNORE` -> `... ON CONFLICT DO NOTHING`,
#   - yields hybrid rows supporting both row[0] and row["col"],
#   - returns the connection to the pool on .close().
# SQLite-only constructs the shim cannot infer (INSERT OR REPLACE, lastrowid,
# strftime) are rewritten at their call sites.
# ---------------------------------------------------------------------------


def _translate_sql(sql: str) -> str:
    """Rewrite SQLite-flavoured SQL to psycopg-compatible SQL."""
    or_ignore = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, re.IGNORECASE)
    if or_ignore:
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)
    # Escape literal % (e.g. LIKE '%x%') before introducing %s placeholders.
    sql = sql.replace("%", "%%").replace("?", "%s")
    if or_ignore and "ON CONFLICT" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


class _HybridRow:
    """A query row supporting both positional (row[0]) and named (row["c"])
    access, matching sqlite3.Row so call sites work unchanged on Postgres."""

    __slots__ = ("_cols", "_values", "_map")

    def __init__(self, cols: list[str], values: tuple):
        self._cols = cols
        self._values = values
        self._map = dict(zip(cols, values, strict=False))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._cols)

    def get(self, key: str, default: Any = None) -> Any:
        return self._map.get(key, default)


def _hybrid_row_factory(cursor: Any):
    cols = [c.name for c in cursor.description] if cursor.description else []

    def make_row(values: tuple) -> _HybridRow:
        return _HybridRow(cols, values)

    return make_row


class _PgCursorAdapter:
    """sqlite3-cursor-like wrapper over a psycopg cursor."""

    def __init__(self, pg_cursor: Any):
        self._cur = pg_cursor

    def execute(self, sql: str, params: Any = ()) -> "_PgCursorAdapter":
        self._cur.execute(_translate_sql(sql), params)
        return self

    def executemany(self, sql: str, seq: Any) -> "_PgCursorAdapter":
        self._cur.executemany(_translate_sql(sql), seq)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> Any:
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> None:
        # Postgres has no implicit lastrowid; call sites needing it use RETURNING.
        return None

    @property
    def description(self) -> Any:
        return self._cur.description


class _PgConnectionAdapter:
    """sqlite3-connection-like wrapper over a pooled psycopg connection."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._conn.row_factory = _hybrid_row_factory

    @property
    def row_factory(self) -> Any:
        return _hybrid_row_factory

    @row_factory.setter
    def row_factory(self, _value: Any) -> None:
        # Call sites set conn.row_factory = sqlite3.Row; we always use hybrid rows.
        pass

    def execute(self, sql: str, params: Any = ()) -> _PgCursorAdapter:
        return _PgCursorAdapter(self._conn.execute(_translate_sql(sql), params))

    def cursor(self) -> _PgCursorAdapter:
        return _PgCursorAdapter(self._conn.cursor())

    def executescript(self, sql: str) -> None:
        self._conn.execute(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        from kroger_mcp.analytics.pg_database import _get_pool

        try:
            self._conn.rollback()
        except Exception:
            pass
        _get_pool().putconn(self._conn)


# Database file location (data directory)
# Create data directory if it doesn't exist
_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
DB_FILE = str(_DATA_DIR / "kroger_analytics.db")

# Global initialization flag
_initialized = False


def get_db_path() -> str:
    """Get the full path to the database file."""
    return DB_FILE


def get_db_connection() -> Any:
    """
    Get a database connection for the active backend.

    SQLite (default) → sqlite3.Connection with row_factory=Row. When DATABASE_URL
    is set → a psycopg-backed adapter exposing the same DB-API surface
    (_PgConnectionAdapter) so call sites are backend-agnostic.
    """
    if get_backend() == "postgresql":
        from kroger_mcp.analytics.pg_database import get_pg_connection

        return _PgConnectionAdapter(get_pg_connection())

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db_cursor():
    """
    Context manager for database operations with automatic commit/rollback.

    Usage:
        with get_db_cursor() as cursor:
            cursor.execute("INSERT INTO ...")
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    """
    Create all database tables if they don't exist.
    """
    conn = get_db_connection()
    try:
        conn.executescript(
            """
            -- Products with category tracking
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                upc TEXT,
                description TEXT,
                brand TEXT,
                ingredients_text TEXT,
                category_type TEXT DEFAULT 'uncategorized',
                category_override INTEGER DEFAULT 0,
                first_purchased_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Every cart add/order event
            CREATE TABLE IF NOT EXISTS purchase_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                event_type TEXT NOT NULL,
                modality TEXT,
                price REAL,
                event_date TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                order_id INTEGER,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Completed orders
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placed_at TEXT NOT NULL,
                item_count INTEGER,
                total_quantity INTEGER,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Pre-computed statistics (updated on each order)
            CREATE TABLE IF NOT EXISTS product_statistics (
                product_id TEXT PRIMARY KEY,
                total_purchases INTEGER DEFAULT 0,
                total_quantity INTEGER DEFAULT 0,
                avg_quantity_per_purchase REAL,
                avg_days_between_purchases REAL,
                std_dev_days REAL,
                last_purchase_date TEXT,
                first_purchase_date TEXT,
                purchase_frequency_score REAL,
                seasonality_score REAL,
                detected_category TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Seasonal patterns for treats
            CREATE TABLE IF NOT EXISTS seasonal_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                month INTEGER NOT NULL,
                week_of_year INTEGER,
                purchase_count INTEGER DEFAULT 0,
                avg_quantity REAL,
                is_peak_period INTEGER DEFAULT 0,
                holiday_association TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                UNIQUE(product_id, month)
            );

            -- Saved recipes
            CREATE TABLE IF NOT EXISTS recipes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                servings INTEGER DEFAULT 4,
                instructions TEXT,
                source TEXT,
                tags TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT,
                last_ordered_at TEXT,
                times_ordered INTEGER DEFAULT 0
            );

            -- Recipe ingredients
            CREATE TABLE IF NOT EXISTS recipe_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id TEXT NOT NULL,
                name TEXT NOT NULL,
                quantity REAL,
                unit TEXT,
                product_id TEXT,
                product_description TEXT,
                category TEXT,
                is_optional INTEGER DEFAULT 0,
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
            );

            -- Pantry inventory tracking
            CREATE TABLE IF NOT EXISTS pantry_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                description TEXT,
                level_percent INTEGER DEFAULT 100,
                last_restocked_at TEXT,
                last_updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                auto_deplete INTEGER DEFAULT 1,
                daily_depletion_rate REAL DEFAULT 0,
                low_threshold INTEGER DEFAULT 20,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Favorite lists (named shopping lists)
            CREATE TABLE IF NOT EXISTS favorite_lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                list_type TEXT DEFAULT 'custom',
                reorder_weeks INTEGER DEFAULT NULL,
                last_ordered_at TEXT DEFAULT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Favorite list items (products in each list)
            CREATE TABLE IF NOT EXISTS favorite_list_items (
                list_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                description TEXT NOT NULL,
                brand TEXT,
                default_quantity INTEGER DEFAULT 1,
                preferred_modality TEXT DEFAULT 'PICKUP',
                notes TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                times_ordered INTEGER DEFAULT 0,
                PRIMARY KEY (list_id, product_id),
                FOREIGN KEY (list_id) REFERENCES favorite_lists(id) ON DELETE CASCADE
            );

            -- Meal plans (weekly, monthly, or custom date ranges)
            CREATE TABLE IF NOT EXISTS meal_plans (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                plan_type TEXT DEFAULT 'weekly',
                is_template INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_ordered_at TEXT,
                times_ordered INTEGER DEFAULT 0
            );

            -- Individual meal entries (recipe assignments to days/slots)
            CREATE TABLE IF NOT EXISTS meal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                meal_slot TEXT NOT NULL,
                servings_override INTEGER,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (plan_id) REFERENCES meal_plans(id) ON DELETE CASCADE,
                UNIQUE(plan_id, meal_date, meal_slot)
            );

            -- Safe products (user-approved, bypass all ingredient checks)
            CREATE TABLE IF NOT EXISTS safe_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                description TEXT,
                brand TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                added_reason TEXT
            );

            -- Blocked products (user-rejected, require explicit confirmation)
            CREATE TABLE IF NOT EXISTS blocked_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                description TEXT,
                blocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                blocked_reason TEXT,
                auto_blocked INTEGER DEFAULT 0
            );

            -- User ingredient preferences (enable/disable specific checks)
            CREATE TABLE IF NOT EXISTS ingredient_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ingredient_key TEXT UNIQUE NOT NULL,
                enabled INTEGER DEFAULT 1,
                severity TEXT DEFAULT 'warning',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Safety settings (global configuration)
            CREATE TABLE IF NOT EXISTS safety_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Price history tracking (for deal discovery and trend analysis)
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                regular_price REAL,
                sale_price REAL,
                on_sale INTEGER DEFAULT 0,
                savings_amount REAL DEFAULT 0,
                savings_percent REAL DEFAULT 0,
                location_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Deal watchlist (user-tracked items for price monitoring)
            CREATE TABLE IF NOT EXISTS deal_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT UNIQUE NOT NULL,
                description TEXT,
                target_price REAL,
                priority INTEGER DEFAULT 1,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TEXT,
                best_price_seen REAL,
                best_price_date TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Whole foods catalog (curated list of clean/natural foods)
            CREATE TABLE IF NOT EXISTS whole_foods_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                description TEXT,
                brand TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                added_by TEXT DEFAULT 'auto',
                safety_status TEXT,
                ingredient_count INTEGER,
                processing_level TEXT,
                notes TEXT,
                last_verified_at TEXT,
                is_currently_available INTEGER DEFAULT 1,
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                UNIQUE(product_id)
            );

            -- Background scan results (deals found during automated scans)
            CREATE TABLE IF NOT EXISTS deal_scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                description TEXT,
                regular_price REAL,
                sale_price REAL,
                savings_amount REAL,
                scan_date TEXT NOT NULL,
                scan_time TEXT NOT NULL,
                viewed INTEGER DEFAULT 0,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            -- Custom ingredients (user-added ingredients beyond defaults)
            CREATE TABLE IF NOT EXISTS custom_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ingredient_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                severity TEXT NOT NULL CHECK(severity IN ('critical', 'warning', 'watch')),
                category TEXT,
                reason TEXT,
                aliases TEXT,  -- JSON array of alternative names
                source TEXT DEFAULT 'user' CHECK(source IN ('user', 'imported', 'system')),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                modified_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
                notes TEXT
            );

            -- Ingredient overrides (modify default/hardcoded ingredients)
            CREATE TABLE IF NOT EXISTS ingredient_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ingredient_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                override_severity TEXT CHECK(override_severity IN ('critical', 'warning', 'watch')),
                override_reason TEXT,
                additional_aliases TEXT,  -- JSON array of extra aliases
                is_hidden INTEGER DEFAULT 0 CHECK(is_hidden IN (0, 1)),
                modified_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notes TEXT
            );

            -- Per-account ingredient->product link memory. Powers smart
            -- auto-linking and learned name standardization. norm_name is the
            -- mechanical grouping key; raw_name is kept verbatim so the most-used
            -- surface form can be surfaced as the account's canonical name.
            CREATE TABLE IF NOT EXISTS ingredient_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                norm_name TEXT NOT NULL,
                raw_name TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_description TEXT,
                times_linked INTEGER NOT NULL DEFAULT 1,
                last_linked_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, norm_name, product_id)
            );

            -- Cook deduction ledger: exact pantry reversal data per cook.
            -- consume_from_pantry's purchase_events.quantity_delta stores the
            -- raw quantity, NOT the percentage points removed from level_percent,
            -- so undo cannot recompute the deduction from purchase_events. This
            -- ledger records the exact deducted_percent per ingredient, keyed by
            -- cook_event_id (meal_entries.id for scheduled cooks, a uuid4 for
            -- ad-hoc 'I made this' cooks) so a cook can be reversed precisely.
            CREATE TABLE IF NOT EXISTS cook_deductions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cook_event_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                product_id TEXT NOT NULL,
                deducted_percent REAL NOT NULL,
                previous_level INTEGER,
                user_id TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Create default favorites list
            INSERT OR IGNORE INTO favorite_lists (id, name, description, list_type)
            VALUES ('default', 'My Favorites', 'Default favorites list', 'custom');

            -- Initialize default safety settings
            INSERT OR IGNORE INTO safety_settings (key, value)
            VALUES ('filtering_enabled', '1');
            INSERT OR IGNORE INTO safety_settings (key, value)
            VALUES ('block_mode', 'soft');

            -- Indexes for performance
            CREATE INDEX IF NOT EXISTS idx_purchase_events_product
                ON purchase_events(product_id);
            CREATE INDEX IF NOT EXISTS idx_purchase_events_date
                ON purchase_events(event_date);
            CREATE INDEX IF NOT EXISTS idx_purchase_events_order
                ON purchase_events(order_id);
            CREATE INDEX IF NOT EXISTS idx_purchase_events_type
                ON purchase_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_seasonal_patterns_product
                ON seasonal_patterns(product_id);
            CREATE INDEX IF NOT EXISTS idx_products_category
                ON products(category_type);
            CREATE INDEX IF NOT EXISTS idx_custom_ingredients_name
                ON custom_ingredients(ingredient_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_custom_ingredients_severity
                ON custom_ingredients(severity);
            CREATE INDEX IF NOT EXISTS idx_custom_ingredients_active
                ON custom_ingredients(is_active);
            CREATE INDEX IF NOT EXISTS idx_ingredient_overrides_name
                ON ingredient_overrides(ingredient_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_ingredient_overrides_hidden
                ON ingredient_overrides(is_hidden);
            CREATE INDEX IF NOT EXISTS idx_ingredient_links_lookup
                ON ingredient_links(user_id, norm_name);
            CREATE INDEX IF NOT EXISTS idx_ingredient_links_canonical
                ON ingredient_links(user_id, norm_name, raw_name);
            CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe
                ON recipe_ingredients(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_pantry_items_product
                ON pantry_items(product_id);
            CREATE INDEX IF NOT EXISTS idx_pantry_items_level
                ON pantry_items(level_percent);
            CREATE INDEX IF NOT EXISTS idx_favorite_list_items_list
                ON favorite_list_items(list_id);
            CREATE INDEX IF NOT EXISTS idx_favorite_list_items_product
                ON favorite_list_items(product_id);
            CREATE INDEX IF NOT EXISTS idx_cook_deductions_event
                ON cook_deductions(cook_event_id, source_type, user_id);
            CREATE INDEX IF NOT EXISTS idx_meal_entries_plan
                ON meal_entries(plan_id);
            CREATE INDEX IF NOT EXISTS idx_meal_entries_date
                ON meal_entries(meal_date);
            CREATE INDEX IF NOT EXISTS idx_meal_plans_dates
                ON meal_plans(start_date, end_date);
            CREATE INDEX IF NOT EXISTS idx_safe_products_product
                ON safe_products(product_id);
            CREATE INDEX IF NOT EXISTS idx_blocked_products_product
                ON blocked_products(product_id);
            CREATE INDEX IF NOT EXISTS idx_ingredient_preferences_key
                ON ingredient_preferences(ingredient_key);
            CREATE INDEX IF NOT EXISTS idx_price_history_product
                ON price_history(product_id);
            CREATE INDEX IF NOT EXISTS idx_price_history_date
                ON price_history(observed_at);
            CREATE INDEX IF NOT EXISTS idx_price_history_on_sale
                ON price_history(on_sale);
            CREATE INDEX IF NOT EXISTS idx_price_history_product_date
                ON price_history(product_id, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_watchlist_priority
                ON deal_watchlist(priority DESC, last_checked_at ASC);
            CREATE INDEX IF NOT EXISTS idx_whole_foods_catalog_product
                ON whole_foods_catalog(product_id);
            CREATE INDEX IF NOT EXISTS idx_whole_foods_catalog_available
                ON whole_foods_catalog(is_currently_available);
            CREATE INDEX IF NOT EXISTS idx_deal_scan_results_date
                ON deal_scan_results(scan_date DESC);
            CREATE INDEX IF NOT EXISTS idx_deal_scan_results_viewed
                ON deal_scan_results(viewed);
        """
        )
        conn.commit()
    finally:
        conn.close()


def _migrate_giant_favorites() -> None:
    """One-time: copy all items from 'Giant Favorites' into default list, then delete it."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id FROM favorite_lists WHERE name = 'Giant Favorites'")
        row = cursor.fetchone()
        if not row:
            return  # already done or never existed

        giant_id = row["id"]

        cursor.execute(
            """
            INSERT OR IGNORE INTO favorite_list_items
                (list_id, product_id, description, brand, default_quantity,
                 preferred_modality, notes, times_ordered,
                 min_stock_percent, min_stock_quantity, current_stock_quantity)
            SELECT
                'default', product_id, description, brand, default_quantity,
                preferred_modality, notes, times_ordered,
                min_stock_percent, min_stock_quantity, current_stock_quantity
            FROM favorite_list_items
            WHERE list_id = ?
            """,
            (giant_id,),
        )

        cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (giant_id,))


def ensure_initialized() -> None:
    """
    Ensure database is initialized and migration is run if needed.

    This should be called before any analytics operations.
    """
    global _initialized
    if _initialized:
        return

    if get_backend() == "postgresql":
        # Postgres builds the complete schema in one shot (SCHEMA_SQL); the
        # SQLite incremental PRAGMA-based migrations + JSON import don't apply.
        from kroger_mcp.analytics.pg_database import initialize_pg_database

        initialize_pg_database()
        _initialized = True
        return

    # Initialize database schema
    initialize_database()

    # Run schema migrations for new columns
    run_schema_migrations()

    # Check if migration is needed
    from .migration import migrate_json_to_sqlite, needs_migration

    if needs_migration():
        migrate_json_to_sqlite()

    # One-time data migration: merge Giant Favorites into default list
    _migrate_giant_favorites()

    _initialized = True


def reset_initialization() -> None:
    """Reset the initialization flag (for testing purposes)."""
    global _initialized
    _initialized = False


async def run_in_thread(func, *args, **kwargs):
    """Run a blocking synchronous function in a thread pool to avoid blocking the event loop.

    Use this to wrap any sync DB or I/O function called from an async handler:
        result = await run_in_thread(some_sync_db_func, arg1, arg2)
    """
    return await asyncio.to_thread(func, *args, **kwargs)


def get_table_counts() -> dict:
    """
    Get row counts for all tables (for diagnostics).

    Returns:
        Dict with table names as keys and row counts as values
    """
    conn = get_db_connection()
    try:
        counts = {}
        for table in [
            "products",
            "purchase_events",
            "orders",
            "product_statistics",
            "seasonal_patterns",
            "recipes",
            "recipe_ingredients",
            "pantry_items",
            "favorite_lists",
            "favorite_list_items",
            "meal_plans",
            "meal_entries",
            "safe_products",
            "blocked_products",
            "ingredient_preferences",
            "safety_settings",
            "price_history",
            "deal_watchlist",
            "whole_foods_catalog",
            "deal_scan_results",
            "ingredient_links",
        ]:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        return counts
    finally:
        conn.close()


def run_schema_migrations() -> None:
    """
    Run schema migrations to add new columns to existing tables.

    This is idempotent - safe to run multiple times.
    """
    conn = get_db_connection()
    try:
        conn.execute("BEGIN")

        # Per-user key/value preferences (location, servings, consent flags, …).
        # Normally created by scripts/migrate_to_multi_tenant.py; created here too
        # so fresh installs and the consent layer work without the full migration.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, setting_key)
            )
            """
        )

        # Get existing columns in product_statistics
        cursor = conn.execute("PRAGMA table_info(product_statistics)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        # Add new columns if they don't exist
        new_columns = [
            ("trend_direction", "TEXT DEFAULT 'stable'"),
            ("trend_strength", "REAL DEFAULT 0.0"),
            ("quantity_adjusted_rate", "REAL DEFAULT NULL"),
            ("prediction_accuracy", "REAL DEFAULT NULL"),
        ]

        for col_name, col_def in new_columns:
            if col_name not in existing_columns:
                conn.execute(f"ALTER TABLE product_statistics ADD COLUMN {col_name} {col_def}")

        # Migrate favorite_lists table - add reorder schedule columns
        cursor = conn.execute("PRAGMA table_info(favorite_lists)")
        favorite_lists_columns = {row[1] for row in cursor.fetchall()}

        favorite_lists_new_columns = [
            ("reorder_weeks", "INTEGER DEFAULT NULL"),
            ("last_ordered_at", "TEXT DEFAULT NULL"),
        ]

        for col_name, col_def in favorite_lists_new_columns:
            if col_name not in favorite_lists_columns:
                conn.execute(f"ALTER TABLE favorite_lists ADD COLUMN {col_name} {col_def}")

        # Migrate pantry_items table - add expiration tracking
        cursor = conn.execute("PRAGMA table_info(pantry_items)")
        pantry_items_columns = {row[1] for row in cursor.fetchall()}

        pantry_items_new_columns = [
            ("expiration_date", "TEXT DEFAULT NULL"),
            ("days_to_expiration", "INTEGER DEFAULT NULL"),
            ("quantity_on_hand", "REAL DEFAULT NULL"),
            ("unit", "TEXT DEFAULT NULL"),
            ("last_used_at", "TEXT DEFAULT NULL"),
            ("last_used_source", "TEXT DEFAULT NULL"),
        ]

        for col_name, col_def in pantry_items_new_columns:
            if col_name not in pantry_items_columns:
                conn.execute(f"ALTER TABLE pantry_items ADD COLUMN {col_name} {col_def}")

        # Enrich purchase_events for source-attributed consumption.
        # event_type CHECK is intentionally NOT added; existing rows use free-form
        # values (order_placed, pantry_depleted) and SQLite cannot ALTER CHECK in place.
        cursor = conn.execute("PRAGMA table_info(purchase_events)")
        purchase_events_columns = {row[1] for row in cursor.fetchall()}
        purchase_events_new_columns = [
            ("recipe_id", "TEXT DEFAULT NULL"),
            ("quantity_delta", "REAL DEFAULT NULL"),
            ("unit", "TEXT DEFAULT NULL"),
            ("source_description", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in purchase_events_new_columns:
            if col_name not in purchase_events_columns:
                conn.execute(f"ALTER TABLE purchase_events ADD COLUMN {col_name} {col_def}")

        # Gap reconciliation: tracks shortfalls where a placed order delivered
        # less of a product than a contributing recipe required.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                recipe_id TEXT,
                recipe_name TEXT,
                product_id TEXT NOT NULL,
                product_description TEXT,
                needed_quantity REAL NOT NULL,
                ordered_quantity REAL NOT NULL,
                unit TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT,
                resolution TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_gaps_user_unresolved "
            "ON pending_gaps(user_id, resolved_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_gaps_product " "ON pending_gaps(product_id)"
        )

        # Migrate favorite_list_items table - add minimum stock tracking
        cursor = conn.execute("PRAGMA table_info(favorite_list_items)")
        fli_columns = {row[1] for row in cursor.fetchall()}

        fli_new_columns = [
            ("min_stock_percent", "INTEGER DEFAULT NULL"),
            ("min_stock_quantity", "INTEGER DEFAULT NULL"),
            ("current_stock_quantity", "INTEGER DEFAULT NULL"),
        ]

        for col_name, col_def in fli_new_columns:
            if col_name not in fli_columns:
                conn.execute(f"ALTER TABLE favorite_list_items ADD COLUMN {col_name} {col_def}")

        # Migrate meal_entries table - add cooking/deduction tracking
        cursor = conn.execute("PRAGMA table_info(meal_entries)")
        meal_entries_columns = {row[1] for row in cursor.fetchall()}

        meal_entries_new_columns = [
            ("cooked_at", "TEXT DEFAULT NULL"),
            ("pantry_deducted", "INTEGER DEFAULT 0"),
        ]

        for col_name, col_def in meal_entries_new_columns:
            if col_name not in meal_entries_columns:
                conn.execute(f"ALTER TABLE meal_entries ADD COLUMN {col_name} {col_def}")

        # Migrate products table - add USDA ingredient text cache
        cursor = conn.execute("PRAGMA table_info(products)")
        products_columns = {row[1] for row in cursor.fetchall()}

        if "ingredients_text" not in products_columns:
            conn.execute("ALTER TABLE products ADD COLUMN ingredients_text TEXT")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
