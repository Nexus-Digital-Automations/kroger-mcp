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
    first_purchased_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Purchase events (user-scoped)
CREATE TABLE IF NOT EXISTS purchase_events (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id VARCHAR(50) NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    event_type VARCHAR(20) NOT NULL,
    modality VARCHAR(20),
    price NUMERIC(10,2),
    event_date DATE NOT NULL,
    event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    order_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pe_user_product ON purchase_events(user_id, product_id);

-- Orders (user-scoped)
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    placed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    item_count INTEGER,
    total_quantity INTEGER,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

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

-- Meal entries
CREATE TABLE IF NOT EXISTS meal_entries (
    id SERIAL PRIMARY KEY,
    plan_id VARCHAR(50) NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
    recipe_id VARCHAR(50) NOT NULL,
    meal_date DATE NOT NULL,
    meal_slot VARCHAR(20) NOT NULL,
    servings_override INTEGER,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
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
    UNIQUE(user_id, product_id)
);

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
