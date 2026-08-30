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
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID


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


# Columns declared BOOLEAN in the Postgres schema. The SQLite-era code compares
# them with the integer idiom (`col = 1`, `SET col = 0`), which Postgres rejects
# ("operator does not exist: boolean = integer"). The adapter normalises those
# literal comparisons to TRUE/FALSE for the PG path only; the SQLite path keeps
# `= 1` (SQLite stores booleans as 0/1). Columns intentionally kept INTEGER in PG
# — is_currently_available, viewed, meal_log_items.pantry_deducted — are NOT here;
# only `= <literal 0|1>` is rewritten, so parameterised values (`= %s`) are
# untouched. Keep in sync with the BOOLEAN columns in pg_database.SCHEMA_SQL.
_PG_BOOL_COLS = (
    "is_active",
    "category_override",
    "is_peak_period",
    "is_optional",
    "auto_deplete",
    "is_template",
    "pantry_deducted",
    "auto_blocked",
    "enabled",
    "on_sale",
    "purchased",
    "is_hidden",
    "cook_skipped",
    "is_manual",
    "manual_purchase",
)
_BOOL_EQ_RE = re.compile(r"\b(" + "|".join(_PG_BOOL_COLS) + r")\s*=\s*([01])\b")


def _normalize_bool_literals(sql: str) -> str:
    """Rewrite `bool_col = 1|0` to `bool_col = TRUE|FALSE` (PG path only)."""
    return _BOOL_EQ_RE.sub(
        lambda m: f"{m.group(1)} = {'TRUE' if m.group(2) == '1' else 'FALSE'}", sql
    )


def _translate_sql(sql: str) -> str:
    """Rewrite SQLite-flavoured SQL to psycopg-compatible SQL."""
    or_ignore = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, re.IGNORECASE)
    if or_ignore:
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)
    # Boolean idiom before placeholder mangling (operates on literal 0/1 only).
    sql = _normalize_bool_literals(sql)
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


def _coerce_pg_value(value: Any) -> Any:
    """Coerce a Postgres-native value to the JSON-serializable shape SQLite gives.

    SQLite returns TEXT for timestamps and REAL for numbers, so the whole app
    (and FastAPI's ``JSONResponse``) assumes str/float — but psycopg returns
    native ``datetime``/``date``/``time``/``Decimal``/``UUID`` objects, which are
    NOT JSON-serializable and 500 any endpoint that returns such a column
    (e.g. ``added_at`` in get_safe_products). Normalise them to SQLite's
    representation so call sites and JSON responses work unchanged. Booleans are
    left as-is (JSON-serializable; Python ``True == 1`` keeps int comparisons working).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # SQLite CURRENT_TIMESTAMP style: space-separated, no 'T'.
        return value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _hybrid_row_factory(cursor: Any):
    cols = [c.name for c in cursor.description] if cursor.description else []

    def make_row(values: tuple) -> _HybridRow:
        return _HybridRow(cols, tuple(_coerce_pg_value(v) for v in values))

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


def insert_returning_id(
    executor: Any, sql: str, params: Any = (), *, id_col: str = "id"
) -> int | None:
    """Run a single-row INSERT and return the new row's auto-increment id.

    Backend-agnostic replacement for ``cursor.lastrowid``, which is always
    ``None`` on the Postgres shim. On Postgres we append ``RETURNING <id_col>``
    and read it from the cursor; on SQLite we keep the historical
    ``cursor.lastrowid`` path (sqlite3's ``lastrowid`` is reliable for a plain
    single-row INSERT and avoids any RETURNING-version assumptions).

    ``executor`` is anything exposing ``.execute(sql, params)`` returning a
    cursor — a connection or a cursor (both the sqlite3 and shim flavours do).
    The INSERT is NOT committed here; the caller owns the transaction.
    """
    if get_backend() == "postgresql":
        stripped = sql.rstrip().rstrip(";")
        cursor = executor.execute(f"{stripped} RETURNING {id_col}", params)
        row = cursor.fetchone()
        return int(row[0]) if row is not None else None
    cursor = executor.execute(sql, params)
    last = cursor.lastrowid
    return int(last) if last is not None else None


def initialize_database() -> None:
    """
    Create all database tables if they don't exist.
    """
    conn = get_db_connection()
    try:
        conn.executescript("""
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

            -- Every cart add/order event (user-scoped; NULL on legacy pre-multi-tenant rows)
            CREATE TABLE IF NOT EXISTS purchase_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
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

            -- Completed orders (user-scoped; NULL on legacy pre-multi-tenant rows)
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                placed_at TEXT NOT NULL,
                item_count INTEGER,
                total_quantity INTEGER,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Pre-computed statistics (updated on each order; user-scoped)
            CREATE TABLE IF NOT EXISTS product_statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                product_id TEXT NOT NULL,
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
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                UNIQUE(user_id, product_id)
            );

            -- Seasonal patterns for treats (user-scoped)
            CREATE TABLE IF NOT EXISTS seasonal_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                product_id TEXT NOT NULL,
                month INTEGER NOT NULL,
                week_of_year INTEGER,
                purchase_count INTEGER DEFAULT 0,
                avg_quantity REAL,
                is_peak_period INTEGER DEFAULT 0,
                holiday_association TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                UNIQUE(user_id, product_id, month)
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
                last_ordered_at TEXT DEFAULT NULL,
                typical_gap_days INTEGER DEFAULT NULL,
                -- Manual (not sold at Kroger) items. product_id stays NOT NULL and
                -- part of the PK; a manual row carries a synthetic 'manual:<uuid>'
                -- id instead (see analytics/favorites.py::new_manual_product_id).
                is_manual INTEGER DEFAULT 0,
                override_reason TEXT DEFAULT NULL,
                -- Where a manual item is bought ('Walmart', 'Indian grocery').
                -- Free text; known vendors are canonicalized on the way in by
                -- analytics/manual_sources.py::normalize_source so their
                -- shopping-list sections don't fragment across spellings.
                manual_source TEXT DEFAULT NULL,
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

            -- Deal watchlist (user-tracked items for price monitoring, user-scoped)
            CREATE TABLE IF NOT EXISTS deal_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                product_id TEXT NOT NULL,
                description TEXT,
                target_price REAL,
                priority INTEGER DEFAULT 1,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TEXT,
                best_price_seen REAL,
                best_price_date TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                UNIQUE(user_id, product_id)
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

            -- Favorite-on-sale alerts (one per user per sale event; feeds the
            -- in-app notification bell). Written by the daily favorites scan.
            CREATE TABLE IF NOT EXISTS favorite_sale_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                product_id TEXT NOT NULL,
                list_id TEXT,
                description TEXT,
                brand TEXT,
                regular_price REAL,
                sale_price REAL,
                savings_percent REAL DEFAULT 0,
                default_quantity REAL DEFAULT 1,
                preferred_modality TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                seen INTEGER DEFAULT 0,
                dismissed INTEGER DEFAULT 0,
                acted INTEGER DEFAULT 0,
                UNIQUE(user_id, product_id, sale_price)
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

            -- Kroger API call meter: per-day aggregated counters so we can see
            -- where the shared app's 10k/day Products budget goes and produce
            -- usage numbers to justify a higher rate tier. One row per
            -- (day, api_family, op_name, outcome); incremented via UPSERT at the
            -- single retry choke point. Best-effort — a dropped increment never
            -- blocks a Kroger call.
            CREATE TABLE IF NOT EXISTS kroger_api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_date TEXT NOT NULL,
                api_family TEXT NOT NULL,
                op_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                call_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(call_date, api_family, op_name, outcome)
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
            CREATE INDEX IF NOT EXISTS idx_purchase_events_product_type
                ON purchase_events(product_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_orders_placed_at
                ON orders(placed_at DESC);
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
            CREATE INDEX IF NOT EXISTS idx_kroger_api_calls_date
                ON kroger_api_calls(call_date);
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
            CREATE INDEX IF NOT EXISTS idx_price_history_location_date
                ON price_history(location_id, observed_at DESC);
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
        """)
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


def _rebuild_table_add_user_id(
    executor: Any, table: str, new_ddl: str, backfill: bool = True
) -> None:
    """Rebuild a SQLite ``table`` into ``new_ddl`` (which adds ``user_id`` and a
    user-scoped composite UNIQUE/PRIMARY KEY).

    Used to migrate a formerly-global table in place — SQLite cannot drop a
    column-level UNIQUE or change the PRIMARY KEY via ALTER, so we
    create-fresh (as ``{table}_new``) → copy the intersection of columns →
    drop the ORIGINAL table → rename ``{table}_new`` into its place.

    Deliberately never renames the original table away first: SQLite
    rewrites OTHER tables' stored FK clause text to follow a rename (e.g.
    ``favorite_list_items.list_id REFERENCES favorite_lists(id)`` would
    become ``REFERENCES favorite_lists_old(id)``), leaving a dangling
    reference once the ``_old`` table is dropped — matches the sequence
    scripts/migrate_to_multi_tenant.py already uses for exactly this reason.

    When ``backfill`` is True (the default), existing rows are attributed to
    the default owner (resolved via ``mcp_user_id()`` — KROGER_MCP_USER_ID,
    then the migration-installed default). When False, existing rows carry
    ``user_id = NULL`` — used for tables where legacy data predates
    multi-tenancy with no reliable attribution signal; they simply won't
    surface in per-user views going forward. SQLite DDL is transactional, so
    the caller's surrounding transaction keeps this atomic.
    """
    old_cols = [r[1] for r in executor.execute(f"PRAGMA table_info({table})").fetchall()]
    new_table_ddl = new_ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_new", 1)
    executor.execute(new_table_ddl)
    new_cols = [r[1] for r in executor.execute(f"PRAGMA table_info({table}_new)").fetchall()]
    # Copy only columns present in both shapes; user_id is set explicitly.
    carried = [c for c in new_cols if c in old_cols and c != "user_id"]
    col_list = ", ".join(carried)
    if backfill:
        from kroger_mcp.auth.dependencies import mcp_user_id

        owner = mcp_user_id()
        executor.execute(
            f"INSERT INTO {table}_new (user_id, {col_list}) " f"SELECT ?, {col_list} FROM {table}",
            (owner,),
        )
    else:
        executor.execute(f"INSERT INTO {table}_new ({col_list}) SELECT {col_list} FROM {table}")
    executor.execute(f"DROP TABLE {table}")
    executor.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

    # Caller runs this with foreign_keys=OFF (see run_schema_migrations) so
    # the DROP TABLE above doesn't cascade-delete rows in dependent tables
    # (e.g. favorite_list_items on favorite_lists) via SQLite's implicit
    # pre-DROP DELETE FROM. Verify that held: check only tables whose DDL
    # actually references `table` — NOT a whole-database foreign_key_check,
    # which would also trip on unrelated pre-existing FK debt elsewhere in
    # a real, long-lived dev/prod DB that has nothing to do with this
    # rebuild.
    dependents = [
        row[0]
        for row in executor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if row[1] and f"REFERENCES{table.upper()}(" in row[1].upper().replace(" ", "")
    ]
    for dep in dependents:
        bad = [
            v
            for v in executor.execute(f"PRAGMA foreign_key_check({dep})").fetchall()
            if v[2] == table
        ]
        if bad:
            raise RuntimeError(f"rebuilding {table} left {len(bad)} dangling reference(s) in {dep}")


def run_schema_migrations() -> None:
    """
    Run schema migrations to add new columns to existing tables.

    This is idempotent - safe to run multiple times.
    """
    conn = get_db_connection()
    try:
        # _rebuild_table_add_user_id (below) does CREATE-new/copy/DROP-old/
        # RENAME on this same connection. get_db_connection() enables
        # foreign_keys=ON, and SQLite's DROP TABLE performs an implicit
        # DELETE FROM the dropped table first when FK enforcement is on —
        # which fires ON DELETE CASCADE against dependents (e.g.
        # favorite_list_items on favorite_lists), silently wiping their rows
        # even though only the parent's shape is meant to change. Foreign-key
        # enforcement can only be toggled outside a transaction, so this must
        # happen before BEGIN. Mirrors scripts/migrate_to_multi_tenant.py's
        # _recreate_table_without_unique, which already does this for exactly
        # the same reason.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        # Per-user key/value preferences (location, servings, consent flags, …).
        # Normally created by scripts/migrate_to_multi_tenant.py; created here too
        # so fresh installs and the consent layer work without the full migration.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, setting_key)
            )
            """)

        # user_carts / user_shopping_lists / user_notion_sync: same gap as
        # user_settings above — these JSON-state-replacing tables only ever
        # existed via scripts/migrate_to_multi_tenant.py's
        # _create_user_scoped_tables(), never in the app's own baseline
        # schema. A fresh install or isolated test DB has no cart/shopping-
        # list/Notion-sync tables at all. Created here too, matching that
        # script's shape exactly, so fresh installs work without the full
        # one-time migration.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_carts (
                user_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                description TEXT,
                quantity INTEGER DEFAULT 1,
                modality TEXT DEFAULT 'PICKUP',
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_carts_user_id ON user_carts(user_id)")

        # regular_price/sale_price: added after the base CREATE TABLE above, so
        # existing installs need the same ADD-COLUMN-if-missing treatment as
        # product_statistics below. Lets calculate_cart_savings() see real
        # numbers instead of always-empty fields.
        cursor = conn.execute("PRAGMA table_info(user_carts)")
        user_carts_columns = {row[1] for row in cursor.fetchall()}
        for col_name in ("regular_price", "sale_price"):
            if col_name not in user_carts_columns:
                conn.execute(f"ALTER TABLE user_carts ADD COLUMN {col_name} REAL DEFAULT NULL")

        conn.execute("""
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
                notes TEXT,
                -- An item the user sources themselves. Derived from a missing
                -- product_id and cached here so a reader doesn't have to
                -- re-derive it; analytics/manual_sources.py::is_manual_item is
                -- the authority.
                manual_purchase INTEGER DEFAULT 0,
                -- Where that item is bought ('Walmart', 'Indian grocery').
                -- Named manual_source, not source, because recipe_source above
                -- already means "which recipe did this come from".
                manual_source TEXT DEFAULT NULL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_shopping_lists_user_id "
            "ON user_shopping_lists(user_id)"
        )
        # Existing databases predate notes/manual_purchase/manual_source.
        cursor = conn.execute("PRAGMA table_info(user_shopping_lists)")
        usl_columns = {row[1] for row in cursor.fetchall()}
        for col_name, col_def in (
            ("notes", "TEXT DEFAULT NULL"),
            ("manual_purchase", "INTEGER DEFAULT 0"),
            ("manual_source", "TEXT DEFAULT NULL"),
        ):
            if col_name not in usl_columns:
                conn.execute(f"ALTER TABLE user_shopping_lists ADD COLUMN {col_name} {col_def}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_notion_sync (
                user_id TEXT PRIMARY KEY,
                notion_database_id TEXT,
                last_sync_at TEXT,
                config_json TEXT
            )
            """)

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

        # meal_plans / meal_entries have the same gap as the tables below: no
        # user_id in the base CREATE TABLE, only added by the one-time
        # scripts/migrate_to_multi_tenant.py. Unlike favorite_lists/
        # pantry_items/custom_ingredients, neither needs its UNIQUE
        # constraint to change (migrate_to_multi_tenant.py handles both via
        # the simple ALTER TABLE ADD COLUMN path, not TABLES_TO_RECREATE), so
        # a plain ADD COLUMN suffices here — no _rebuild_table_add_user_id
        # rename dance needed. No backfill: same "no reliable attribution for
        # pre-existing rows" reasoning as the rebuilt tables.
        for table in ("meal_plans", "meal_entries"):
            cursor = conn.execute(f"PRAGMA table_info({table})")
            if "user_id" not in {row[1] for row in cursor.fetchall()}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user_id ON {table}(user_id)")

        # favorite_lists has the same gap as pantry_items above: no user_id in
        # the base CREATE TABLE, only added by the one-time
        # scripts/migrate_to_multi_tenant.py against the real dev/prod DB.
        # get_favorite_depletion_rates() (favorite_depletion.py) joins on
        # fl.user_id, so a fresh install needs this too. The base schema's
        # `name TEXT NOT NULL UNIQUE` also has to become non-unique — two
        # different users must each be able to have a "Weekly Staples" list —
        # matching migrate_to_multi_tenant.py's TABLES_TO_RECREATE shape.
        # backfill=False: the only pre-existing row at this point is the
        # generic 'default' seed row (INSERT OR IGNORE above), not real user
        # data — same "no reliable attribution" reasoning as purchase_events/
        # orders above. Backfilling it to the resolved default owner instead
        # would attribute a global seed row to a specific user_id that may not
        # even exist in the users table yet (real installs already carry a
        # migrated user_id here from migrate_to_multi_tenant.py; this path
        # only fires for fresh/test DBs that never ran it).
        cursor = conn.execute("PRAGMA table_info(favorite_lists)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            _rebuild_table_add_user_id(
                conn,
                "favorite_lists",
                """
                CREATE TABLE favorite_lists (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    list_type TEXT DEFAULT 'custom',
                    reorder_weeks INTEGER DEFAULT NULL,
                    last_ordered_at TEXT DEFAULT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    user_id TEXT
                )
                """,
                backfill=False,
            )

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

        # custom_ingredients has the same gap as favorite_lists above: no
        # user_id in the base CREATE TABLE, only added (plus the UNIQUE
        # constraint becoming composite UNIQUE(user_id, ingredient_name)) by
        # the one-time scripts/migrate_to_multi_tenant.py against the real
        # dev/prod DB. The app doesn't yet filter custom_ingredients by
        # user_id (ingredients.py treats it as global), but the schema shape
        # must still match already-migrated installs for a fresh/test DB to
        # behave the same way. backfill=False: no reliable attribution for
        # any pre-existing rows, same reasoning as favorite_lists.
        cursor = conn.execute("PRAGMA table_info(custom_ingredients)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            _rebuild_table_add_user_id(
                conn,
                "custom_ingredients",
                """
                CREATE TABLE custom_ingredients (
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
                )
                """,
                backfill=False,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_custom_ingredients_name "
                "ON custom_ingredients(ingredient_name COLLATE NOCASE)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_custom_ingredients_severity "
                "ON custom_ingredients(severity)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_custom_ingredients_active "
                "ON custom_ingredients(is_active)"
            )

        # pantry_items was never given user_id in the base CREATE TABLE (only
        # scripts/migrate_to_multi_tenant.py added it, one-time, against the
        # real dev/prod DB) — a fresh install's pantry_items has no user_id at
        # all, which every user-scoped pantry function (add_to_pantry,
        # get_pantry_status, get_favorite_depletion_rates, ...) assumes exists.
        # Rebuild it here too, matching that script's target shape exactly, so
        # fresh installs match already-migrated ones. Must run BEFORE the
        # column-addition pass below, or a fresh install's rebuild (which
        # doesn't know about expiration_date/quantity_on_hand/etc. yet) would
        # silently drop those columns on an already-migrated DB.
        cursor = conn.execute("PRAGMA table_info(pantry_items)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            _rebuild_table_add_user_id(
                conn,
                "pantry_items",
                """
                CREATE TABLE pantry_items (
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
                )
                """,
                backfill=False,
            )

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
        # user_id: legacy rows predate multi-tenancy with no reliable attribution
        # signal, so they're left NULL rather than backfilled to one user.
        cursor = conn.execute("PRAGMA table_info(purchase_events)")
        purchase_events_columns = {row[1] for row in cursor.fetchall()}
        purchase_events_new_columns = [
            ("user_id", "TEXT DEFAULT NULL"),
            ("recipe_id", "TEXT DEFAULT NULL"),
            ("quantity_delta", "REAL DEFAULT NULL"),
            ("unit", "TEXT DEFAULT NULL"),
            ("source_description", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in purchase_events_new_columns:
            if col_name not in purchase_events_columns:
                conn.execute(f"ALTER TABLE purchase_events ADD COLUMN {col_name} {col_def}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_purchase_events_user_product "
            "ON purchase_events(user_id, product_id)"
        )

        # orders: same legacy-unattributed treatment as purchase_events.
        cursor = conn.execute("PRAGMA table_info(orders)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            conn.execute("ALTER TABLE orders ADD COLUMN user_id TEXT DEFAULT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")

        # Gap reconciliation: tracks shortfalls where a placed order delivered
        # less of a product than a contributing recipe required.
        conn.execute("""
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
            """)
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
            # Snack replenishment tracking (no fixed reorder schedule):
            # last_ordered_at drives the staleness signal; typical_gap_days is
            # the per-item "days between buys" threshold (defaults to 21 in app).
            ("last_ordered_at", "TEXT DEFAULT NULL"),
            ("typical_gap_days", "INTEGER DEFAULT NULL"),
            # Manual items — no Kroger product behind them (see schema comment).
            ("is_manual", "INTEGER DEFAULT 0"),
            ("override_reason", "TEXT DEFAULT NULL"),
            # Vendor for a manual item (see schema comment).
            ("manual_source", "TEXT DEFAULT NULL"),
        ]

        added_fli_columns = []
        for col_name, col_def in fli_new_columns:
            if col_name not in fli_columns:
                conn.execute(f"ALTER TABLE favorite_list_items ADD COLUMN {col_name} {col_def}")
                added_fli_columns.append(col_name)

        # One-time backfill: seed last_ordered_at for pre-existing favorites from
        # the pantry's last restock date so snacks aren't all cold-start "never
        # ordered" the first time the check-up runs. Only fires the migration
        # pass that first adds the column.
        if "last_ordered_at" in added_fli_columns:
            conn.execute("""
                UPDATE favorite_list_items
                SET last_ordered_at = (
                    SELECT pi.last_restocked_at
                    FROM pantry_items pi
                    WHERE pi.product_id = favorite_list_items.product_id
                )
                WHERE last_ordered_at IS NULL
                  AND EXISTS (
                    SELECT 1 FROM pantry_items pi
                    WHERE pi.product_id = favorite_list_items.product_id
                      AND pi.last_restocked_at IS NOT NULL
                  )
                """)

        # Migrate meal_entries table - add cooking/deduction tracking
        cursor = conn.execute("PRAGMA table_info(meal_entries)")
        meal_entries_columns = {row[1] for row in cursor.fetchall()}

        meal_entries_new_columns = [
            ("cooked_at", "TEXT DEFAULT NULL"),
            ("pantry_deducted", "INTEGER DEFAULT 0"),
            # Tombstone set when a user undoes a past meal ("I didn't cook this"),
            # so the lazy reconciler never silently re-deducts it.
            ("cook_skipped", "INTEGER DEFAULT 0"),
        ]

        for col_name, col_def in meal_entries_new_columns:
            if col_name not in meal_entries_columns:
                conn.execute(f"ALTER TABLE meal_entries ADD COLUMN {col_name} {col_def}")

        # Migrate products table - add USDA ingredient text cache
        cursor = conn.execute("PRAGMA table_info(products)")
        products_columns = {row[1] for row in cursor.fetchall()}

        if "ingredients_text" not in products_columns:
            conn.execute("ALTER TABLE products ADD COLUMN ingredients_text TEXT")

        # User-scope deal_watchlist + seasonal_patterns (were global). SQLite can't
        # drop a column-level UNIQUE in place, so each is rebuilt with user_id and a
        # composite UNIQUE, backfilling existing rows to the default owner. Idempotent:
        # the rebuild only fires while the legacy (user_id-less) shape is present.
        cursor = conn.execute("PRAGMA table_info(deal_watchlist)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            _rebuild_table_add_user_id(
                conn,
                "deal_watchlist",
                """
                CREATE TABLE deal_watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    product_id TEXT NOT NULL,
                    description TEXT,
                    target_price REAL,
                    priority INTEGER DEFAULT 1,
                    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_checked_at TEXT,
                    best_price_seen REAL,
                    best_price_date TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(product_id),
                    UNIQUE(user_id, product_id)
                )
                """,
            )

        cursor = conn.execute("PRAGMA table_info(seasonal_patterns)")
        if "user_id" not in {row[1] for row in cursor.fetchall()}:
            _rebuild_table_add_user_id(
                conn,
                "seasonal_patterns",
                """
                CREATE TABLE seasonal_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    product_id TEXT NOT NULL,
                    month INTEGER NOT NULL,
                    week_of_year INTEGER,
                    purchase_count INTEGER DEFAULT 0,
                    avg_quantity REAL,
                    is_peak_period INTEGER DEFAULT 0,
                    holiday_association TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(product_id),
                    UNIQUE(user_id, product_id, month)
                )
                """,
            )

        # product_statistics: was globally keyed on product_id alone. SQLite can't
        # change a PRIMARY KEY via ALTER, so it's rebuilt with a surrogate id +
        # user-scoped UNIQUE. It's a pre-computed/derived cache table, so legacy
        # rows are carried forward unattributed (user_id NULL) rather than
        # backfilled — they'll be superseded as update_all_product_stats recomputes
        # per user going forward.
        #
        # Checked by PK shape, not column presence: an earlier one-time script
        # (scripts/migrate_to_multi_tenant.py) already bolted a bare `user_id`
        # column onto this table without rebuilding the PRIMARY KEY, so a
        # column-presence check would wrongly skip the rebuild and leave the
        # table without the UNIQUE(user_id, product_id) constraint callers rely on.
        cursor = conn.execute("PRAGMA table_info(product_statistics)")
        if any(row[1] == "product_id" and row[5] == 1 for row in cursor.fetchall()):
            _rebuild_table_add_user_id(
                conn,
                "product_statistics",
                """
                CREATE TABLE product_statistics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    product_id TEXT NOT NULL,
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
                    trend_direction TEXT DEFAULT 'stable',
                    trend_strength REAL DEFAULT 0.0,
                    quantity_adjusted_rate REAL DEFAULT NULL,
                    prediction_accuracy REAL DEFAULT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(product_id),
                    UNIQUE(user_id, product_id)
                )
                """,
                backfill=False,
            )

        # _rebuild_table_add_user_id (above) already verifies its own
        # dependents are intact before returning; nothing further to check
        # here. (Not a whole-DB PRAGMA foreign_key_check: a real long-lived
        # dev/prod DB can carry unrelated pre-existing FK debt in tables
        # this migration never touches, which would fail this step for
        # reasons that have nothing to do with the migration.)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
