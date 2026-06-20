"""PostgreSQL database layer for multi-user Kroger MCP.

Provides connection pooling, schema creation, and migration support.
Activated when DATABASE_URL environment variable is set.
Falls back to SQLite (database.py) when DATABASE_URL is not set.
"""

import os
from contextlib import contextmanager

_pool = None


def get_database_url() -> str | None:
    """Get PostgreSQL connection URL from environment."""
    return os.environ.get("DATABASE_URL")


def _get_pool():
    """Get or create the connection pool (lazy singleton)."""
    global _pool
    if _pool is None:
        import psycopg_pool

        url = get_database_url()
        if not url:
            raise RuntimeError("DATABASE_URL not set — cannot use PostgreSQL backend")
        # Per-worker pool. Sized so (workers x max_size) stays well under
        # Postgres max_connections: 4 workers x 8 = 32 < 50/100. Lazy-init here
        # keeps the pool's sockets inside the worker process (fork-safe).
        min_size = int(os.environ.get("PG_POOL_MIN", 1))
        max_size = int(os.environ.get("PG_POOL_MAX", 8))
        _pool = psycopg_pool.ConnectionPool(
            url, min_size=min_size, max_size=max_size, timeout=10
        )
    return _pool


def get_pg_connection():
    """Get a PostgreSQL connection from the pool.

    Caller must close the connection when done (returns it to pool).
    """
    pool = _get_pool()
    return pool.getconn()


@contextmanager
def get_pg_cursor(commit: bool = True):
    """Context manager for a PostgreSQL cursor.

    Usage:
        with get_pg_cursor() as cur:
            cur.execute("SELECT ...")
    """
    conn = get_pg_connection()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        _get_pool().putconn(conn)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Users and authentication
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    kroger_profile_id VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ip_address VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);

-- Products (global catalog — no user_id)
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(50) UNIQUE NOT NULL,
    upc VARCHAR(50),
    description TEXT,
    brand VARCHAR(255),
    category_type VARCHAR(50) DEFAULT 'uncategorized',
    category_override BOOLEAN DEFAULT FALSE,
    -- USDA / label ingredient-text cache (SQLite adds this via run_schema_migrations).
    ingredients_text TEXT,
    first_purchased_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Purchase events (user-scoped). user_id is nullable to match SQLite runtime:
-- record_cart_add / record_order INSERT without a user_id (the multi-tenant
-- migration left these writes unscoped; new rows carry NULL on SQLite too).
-- Consumption-attribution columns (recipe_id, quantity_delta, unit,
-- source_description) are added by run_schema_migrations on SQLite.
CREATE TABLE IF NOT EXISTS purchase_events (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    event_type VARCHAR(20) NOT NULL,
    modality VARCHAR(20),
    price NUMERIC(10,2),
    event_date DATE NOT NULL,
    event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    order_id INTEGER,
    recipe_id VARCHAR(50),
    quantity_delta NUMERIC(10,3),
    unit VARCHAR(50),
    source_description TEXT
);
CREATE INDEX IF NOT EXISTS idx_pe_user_product ON purchase_events(user_id, product_id);
-- Perf: date-range reports/predictions filter on event_date; per-product event
-- filters need (product_id, event_type); order-history joins on order_id.
CREATE INDEX IF NOT EXISTS idx_pe_event_date ON purchase_events(event_date DESC);
CREATE INDEX IF NOT EXISTS idx_pe_product_event_type ON purchase_events(product_id, event_type);
CREATE INDEX IF NOT EXISTS idx_pe_order_id ON purchase_events(order_id);

-- Orders (user-scoped). user_id nullable to match SQLite runtime: record_order
-- INSERTs without a user_id (same unscoped-write gap as purchase_events).
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    placed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    item_count INTEGER,
    total_quantity INTEGER,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_placed_at ON orders(placed_at DESC);

-- Product statistics (user-scoped)
CREATE TABLE IF NOT EXISTS product_statistics (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    total_purchases INTEGER DEFAULT 0,
    total_quantity INTEGER DEFAULT 0,
    avg_quantity_per_purchase NUMERIC(8,2),
    avg_days_between_purchases NUMERIC(8,2),
    std_dev_days NUMERIC(8,2),
    last_purchase_date DATE,
    first_purchase_date DATE,
    purchase_frequency_score NUMERIC(8,4),
    seasonality_score NUMERIC(8,4),
    detected_category VARCHAR(50),
    trend_direction VARCHAR(20) DEFAULT 'stable',
    trend_strength NUMERIC(8,4) DEFAULT 0.0,
    quantity_adjusted_rate NUMERIC(8,4),
    prediction_accuracy NUMERIC(8,4),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

-- Seasonal patterns
CREATE TABLE IF NOT EXISTS seasonal_patterns (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    month INTEGER NOT NULL,
    week_of_year INTEGER,
    purchase_count INTEGER DEFAULT 0,
    avg_quantity NUMERIC(8,2),
    is_peak_period BOOLEAN DEFAULT FALSE,
    holiday_association VARCHAR(100),
    UNIQUE(user_id, product_id, month)
);

-- Recipes (user-scoped)
CREATE TABLE IF NOT EXISTS recipes (
    id VARCHAR(50) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(500) NOT NULL,
    description TEXT,
    servings INTEGER DEFAULT 4,
    instructions TEXT,
    source VARCHAR(500),
    tags TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE,
    last_ordered_at TIMESTAMP WITH TIME ZONE,
    times_ordered INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recipes_user ON recipes(user_id);

-- Recipe ingredients
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id SERIAL PRIMARY KEY,
    recipe_id VARCHAR(50) NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    name VARCHAR(500) NOT NULL,
    quantity NUMERIC(10,3),
    unit VARCHAR(50),
    product_id VARCHAR(50),
    product_description TEXT,
    category VARCHAR(50),
    is_optional BOOLEAN DEFAULT FALSE
);

-- Pantry items (user-scoped)
CREATE TABLE IF NOT EXISTS pantry_items (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    level_percent INTEGER DEFAULT 100,
    last_restocked_at TIMESTAMP WITH TIME ZONE,
    last_updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    auto_deplete BOOLEAN DEFAULT TRUE,
    daily_depletion_rate NUMERIC(8,4) DEFAULT 0,
    low_threshold INTEGER DEFAULT 20,
    expiration_date DATE,
    days_to_expiration INTEGER,
    -- Absolute-count tracking + last-use attribution (SQLite adds these via
    -- run_schema_migrations; add_to_pantry / consume_from_pantry write them).
    quantity_on_hand NUMERIC(10,3),
    unit VARCHAR(50),
    last_used_at TIMESTAMP WITH TIME ZONE,
    last_used_source VARCHAR(50),
    UNIQUE(user_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_pantry_user ON pantry_items(user_id);

-- Favorite lists (user-scoped)
CREATE TABLE IF NOT EXISTS favorite_lists (
    id VARCHAR(50) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    list_type VARCHAR(50) DEFAULT 'custom',
    reorder_weeks INTEGER,
    last_ordered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, name)
);

-- Favorite list items
CREATE TABLE IF NOT EXISTS favorite_list_items (
    list_id VARCHAR(50) NOT NULL REFERENCES favorite_lists(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description VARCHAR(500) NOT NULL,
    brand VARCHAR(255),
    default_quantity INTEGER DEFAULT 1,
    preferred_modality VARCHAR(20) DEFAULT 'PICKUP',
    notes TEXT,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    times_ordered INTEGER DEFAULT 0,
    min_stock_percent INTEGER,
    min_stock_quantity INTEGER,
    current_stock_quantity INTEGER,
    PRIMARY KEY (list_id, product_id)
);

-- Meal plans (user-scoped)
CREATE TABLE IF NOT EXISTS meal_plans (
    id VARCHAR(50) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    plan_type VARCHAR(20) DEFAULT 'weekly',
    is_template BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_ordered_at TIMESTAMP WITH TIME ZONE,
    times_ordered INTEGER DEFAULT 0
);

-- Meal entries (user-scoped). user_id, cooked_at and pantry_deducted are added
-- to SQLite by the multi-tenant migration + run_schema_migrations; meal_planning
-- filters every read by user_id and toggles the cook-tracking columns.
CREATE TABLE IF NOT EXISTS meal_entries (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    plan_id VARCHAR(50) NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
    recipe_id VARCHAR(50) NOT NULL,
    meal_date DATE NOT NULL,
    meal_slot VARCHAR(20) NOT NULL,
    servings_override INTEGER,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    cooked_at TIMESTAMP WITH TIME ZONE,
    pantry_deducted BOOLEAN DEFAULT FALSE,
    UNIQUE(plan_id, meal_date, meal_slot)
);

-- Safe products (user-scoped)
CREATE TABLE IF NOT EXISTS safe_products (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    brand VARCHAR(255),
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    added_reason TEXT,
    UNIQUE(user_id, product_id)
);

-- Blocked products (user-scoped)
CREATE TABLE IF NOT EXISTS blocked_products (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    blocked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    blocked_reason TEXT,
    auto_blocked BOOLEAN DEFAULT FALSE,
    UNIQUE(user_id, product_id)
);

-- Ingredient preferences (user-scoped)
CREATE TABLE IF NOT EXISTS ingredient_preferences (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ingredient_key VARCHAR(100) NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    severity VARCHAR(20) DEFAULT 'warning',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, ingredient_key)
);

-- Safety settings (user-scoped)
CREATE TABLE IF NOT EXISTS safety_settings (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key VARCHAR(100) NOT NULL,
    value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY(user_id, key)
);

-- Price history (global — prices are the same for all users at a location)
CREATE TABLE IF NOT EXISTS price_history (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(50) NOT NULL,
    regular_price NUMERIC(10,2),
    sale_price NUMERIC(10,2),
    on_sale BOOLEAN DEFAULT FALSE,
    savings_amount NUMERIC(10,2) DEFAULT 0,
    savings_percent NUMERIC(5,2) DEFAULT 0,
    location_id VARCHAR(50) NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_price_product ON price_history(product_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_price_location_date ON price_history(location_id, observed_at DESC);

-- Deal watchlist (user-scoped)
CREATE TABLE IF NOT EXISTS deal_watchlist (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    target_price NUMERIC(10,2),
    priority INTEGER DEFAULT 1,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_checked_at TIMESTAMP WITH TIME ZONE,
    last_alert_at TIMESTAMP WITH TIME ZONE,
    -- Best price ever seen for the watched product (deal_tools.add_to_watchlist
    -- writes these; present in the SQLite schema, were absent on PG).
    best_price_seen NUMERIC(10,2),
    best_price_date TIMESTAMP WITH TIME ZONE,
    UNIQUE(user_id, product_id)
);

-- Favorite-on-sale alerts (one per user per sale event; feeds the in-app
-- notification bell). Written by the daily favorites scan.
CREATE TABLE IF NOT EXISTS favorite_sale_alerts (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    list_id TEXT,
    description TEXT,
    brand TEXT,
    regular_price NUMERIC(10,2),
    sale_price NUMERIC(10,2),
    savings_percent NUMERIC(5,2) DEFAULT 0,
    default_quantity NUMERIC(10,2) DEFAULT 1,
    preferred_modality VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    -- 0/1 flags kept INTEGER (not BOOLEAN) so the SQLite `= 0|1` idiom stays
    -- portable without adding these to the adapter's _PG_BOOL_COLS list.
    seen INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0,
    acted INTEGER DEFAULT 0,
    UNIQUE(user_id, product_id, sale_price)
);
CREATE INDEX IF NOT EXISTS idx_fav_alerts_user
    ON favorite_sale_alerts(user_id, dismissed, created_at DESC);

-- User preferences (replaces kroger_preferences.json)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    preferred_location_id VARCHAR(50),
    default_servings INTEGER DEFAULT 4,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Kroger OAuth tokens (user-scoped, encrypted)
CREATE TABLE IF NOT EXISTS kroger_tokens (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_type VARCHAR(50) DEFAULT 'Bearer',
    expires_at TIMESTAMP WITH TIME ZONE,
    scope TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Per-user key/value settings (location, servings, consent flags, …).
-- Mirrors the SQLite user_settings table. The composite PK (user_id,
-- setting_key) is the exact conflict target the consent layer's
-- _save_preference upsert relies on: ON CONFLICT(user_id, setting_key).
CREATE TABLE IF NOT EXISTS user_settings (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    setting_key VARCHAR(100) NOT NULL,
    setting_value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (user_id, setting_key)
);

-- Per-user current cart (mirrors the SQLite table created by
-- migrate_to_multi_tenant). cart_tools._save_cart_data replaces the rows for a
-- user (DELETE then upsert); PK (user_id, product_id) is the conflict target.
CREATE TABLE IF NOT EXISTS user_carts (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    quantity INTEGER DEFAULT 1,
    modality VARCHAR(20) DEFAULT 'PICKUP',
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (user_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_user_carts_user_id ON user_carts(user_id);

-- Per-user shopping list (mirrors the SQLite table created by
-- migrate_to_multi_tenant). shopping_list_tools._save_shopping_list replaces a
-- user's rows; the surrogate id is the PK / upsert conflict target.
CREATE TABLE IF NOT EXISTS user_shopping_lists (
    id VARCHAR(64) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50),
    name VARCHAR(500) NOT NULL,
    quantity NUMERIC(10,3) DEFAULT 1.0,
    unit VARCHAR(50) DEFAULT '',
    category VARCHAR(100),
    purchased BOOLEAN DEFAULT FALSE,
    recipe_source TEXT,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_shopping_lists_user_id
    ON user_shopping_lists(user_id);

-- Per-account ingredient->product link memory (smart auto-linking, learned
-- name standardization). norm_name is the mechanical grouping key; raw_name is
-- the verbatim surface form. UNIQUE(user_id, norm_name, product_id) mirrors the
-- SQLite upsert key.
CREATE TABLE IF NOT EXISTS ingredient_links (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    norm_name VARCHAR(500) NOT NULL,
    raw_name VARCHAR(500) NOT NULL,
    product_id VARCHAR(50) NOT NULL,
    product_description TEXT,
    times_linked INTEGER NOT NULL DEFAULT 1,
    last_linked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(user_id, norm_name, product_id)
);
CREATE INDEX IF NOT EXISTS idx_ingredient_links_user ON ingredient_links(user_id);

-- Custom ingredients (user-added entries beyond defaults). User-scoped per the
-- multi-tenant migration: UNIQUE(user_id, ingredient_name). NOTE: SQLite uses
-- COLLATE NOCASE on ingredient_name (case-insensitive uniqueness); PG uniqueness
-- here is case-sensitive — callers normalize case at the app layer.
CREATE TABLE IF NOT EXISTS custom_ingredients (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ingredient_name VARCHAR(255) NOT NULL,
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('critical', 'warning', 'watch')),
    category VARCHAR(100),
    reason TEXT,
    aliases TEXT,
    source VARCHAR(20) DEFAULT 'user' CHECK (source IN ('user', 'imported', 'system')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    modified_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT,
    UNIQUE(user_id, ingredient_name)
);

-- Ingredient overrides (modify default/hardcoded ingredients). User-scoped per
-- the multi-tenant migration: UNIQUE(user_id, ingredient_name). Same NOCASE
-- caveat as custom_ingredients.
CREATE TABLE IF NOT EXISTS ingredient_overrides (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ingredient_name VARCHAR(255) NOT NULL,
    override_severity VARCHAR(20) CHECK (override_severity IN ('critical', 'warning', 'watch')),
    override_reason TEXT,
    additional_aliases TEXT,
    is_hidden BOOLEAN DEFAULT FALSE,
    modified_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    notes TEXT,
    UNIQUE(user_id, ingredient_name)
);

-- Gap reconciliation: shortfalls where a placed order delivered less of a
-- product than a contributing recipe required (user-scoped, append-only log).
CREATE TABLE IF NOT EXISTS pending_gaps (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipe_id VARCHAR(50),
    recipe_name VARCHAR(500),
    product_id VARCHAR(50) NOT NULL,
    product_description TEXT,
    needed_quantity NUMERIC(10,3) NOT NULL,
    ordered_quantity NUMERIC(10,3) NOT NULL,
    unit VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolution VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_pending_gaps_user_unresolved
    ON pending_gaps(user_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_pending_gaps_product ON pending_gaps(product_id);

-- Cook deduction ledger: exact pantry-reversal data per cook (user-scoped,
-- append-only). deducted_percent records the percentage points removed from
-- pantry level_percent so a cook can be reversed precisely; cook_event_id is a
-- meal_entries.id (scheduled cooks) or a uuid4 (ad-hoc 'I made this').
CREATE TABLE IF NOT EXISTS cook_deductions (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cook_event_id VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    product_id VARCHAR(50) NOT NULL,
    deducted_percent NUMERIC(8,4) NOT NULL,
    previous_level INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cook_deductions_event
    ON cook_deductions(user_id, cook_event_id);

-- ---------------------------------------------------------------------------
-- Tables that exist in the live SQLite runtime but were missing from this PG
-- schema. The ETL migrates only tables present in BOTH backends, so an absent
-- PG table would (a) silently drop the source rows and (b) crash the running
-- app on first write. Reconciled here to mirror the SQLite shapes. NOTE:
-- is_currently_available / viewed / pantry_deducted stay INTEGER (not BOOLEAN)
-- because the app queries them as `= 1` / `= 0` (e.g. product_tools.py:1135);
-- product_id keeps no FK (matches the rest of this schema).
-- ---------------------------------------------------------------------------

-- Curated Whole-Foods-eligible product catalog (global). Actively written by
-- product_tools.py (INSERT … ON CONFLICT(product_id)).
CREATE TABLE IF NOT EXISTS whole_foods_catalog (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    brand TEXT,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    added_by VARCHAR(50) DEFAULT 'auto',
    safety_status VARCHAR(50),
    ingredient_count INTEGER,
    processing_level VARCHAR(50),
    notes TEXT,
    last_verified_at TIMESTAMP WITH TIME ZONE,
    is_currently_available INTEGER DEFAULT 1,
    UNIQUE(product_id)
);
CREATE INDEX IF NOT EXISTS idx_whole_foods_catalog_product
    ON whole_foods_catalog(product_id);
CREATE INDEX IF NOT EXISTS idx_whole_foods_catalog_available
    ON whole_foods_catalog(is_currently_available);

-- Ephemeral deal-scan cache (global). Regenerable; viewed is an int flag.
CREATE TABLE IF NOT EXISTS deal_scan_results (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    regular_price NUMERIC(10,2),
    sale_price NUMERIC(10,2),
    savings_amount NUMERIC(10,2),
    scan_date TEXT NOT NULL,
    scan_time TEXT NOT NULL,
    viewed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deal_scan_results_date
    ON deal_scan_results(scan_date);
CREATE INDEX IF NOT EXISTS idx_deal_scan_results_viewed
    ON deal_scan_results(viewed);

-- Legacy meal log + items (user-scoped). No writers remain in the codebase
-- (superseded by meal_entries / cook_deductions); created for schema parity.
CREATE TABLE IF NOT EXISTS meal_log (
    id SERIAL PRIMARY KEY,
    logged_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    meal_type VARCHAR(20) NOT NULL
        CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'snack')),
    description TEXT,
    recipe_id VARCHAR(50),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meal_log_date ON meal_log(logged_at);
CREATE INDEX IF NOT EXISTS idx_meal_log_type ON meal_log(meal_type);
CREATE INDEX IF NOT EXISTS idx_meal_log_user_id ON meal_log(user_id);

CREATE TABLE IF NOT EXISTS meal_log_items (
    id SERIAL PRIMARY KEY,
    meal_log_id INTEGER NOT NULL REFERENCES meal_log(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    description TEXT,
    quantity_percent NUMERIC(10,3) NOT NULL DEFAULT 10.0,
    previous_level NUMERIC(10,3),
    pantry_deducted INTEGER DEFAULT 0,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meal_log_items_meal ON meal_log_items(meal_log_id);
CREATE INDEX IF NOT EXISTS idx_meal_log_items_product ON meal_log_items(product_id);
CREATE INDEX IF NOT EXISTS idx_meal_log_items_user_id ON meal_log_items(user_id);

-- Legacy pantry consumption ledger (user-scoped). No writers remain (superseded
-- by cook_deductions / purchase_events); created for schema parity.
CREATE TABLE IF NOT EXISTS pantry_consumption_log (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(50) NOT NULL,
    quantity_consumed NUMERIC(10,3) NOT NULL,
    unit VARCHAR(50),
    consumed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    source_id VARCHAR(100),
    source_description TEXT,
    notes TEXT,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_consumption_log_product
    ON pantry_consumption_log(product_id);
CREATE INDEX IF NOT EXISTS idx_consumption_log_date
    ON pantry_consumption_log(consumed_at);
CREATE INDEX IF NOT EXISTS idx_pantry_consumption_log_user_id
    ON pantry_consumption_log(user_id);

-- Per-user Notion sync config (keyed by user). Mirrors the SQLite table.
CREATE TABLE IF NOT EXISTS user_notion_sync (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    notion_database_id TEXT,
    last_sync_at TIMESTAMP WITH TIME ZONE,
    config_json TEXT
);
"""


# Also create SQLite-compatible session/user tables for dev mode
SQLITE_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    kroger_profile_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    ip_address TEXT
);

-- Kroger OAuth tokens (user-scoped, encrypted at rest). Mirrors the PG
-- kroger_tokens table; access_token/refresh_token hold Fernet ciphertext.
CREATE TABLE IF NOT EXISTS kroger_tokens (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_type TEXT DEFAULT 'Bearer',
    expires_at TEXT,
    scope TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def initialize_pg_database() -> None:
    """Create all PostgreSQL tables if they don't exist."""
    conn = get_pg_connection()
    try:
        conn.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        _get_pool().putconn(conn)


def initialize_sqlite_auth_tables() -> None:
    """Create auth tables in SQLite for dev mode."""
    from kroger_mcp.analytics.database import get_db_connection

    conn = get_db_connection()
    try:
        conn.executescript(SQLITE_AUTH_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def close_pool() -> None:
    """Close the connection pool on shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
