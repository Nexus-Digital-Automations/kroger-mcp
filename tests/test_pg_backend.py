"""End-to-end data-path validation of the PostgreSQL backend.

The backend-aware connection shim in ``kroger_mcp.analytics.database`` lets the
SQLite-flavoured analytics/tools layer run unchanged on Postgres. This module
proves that the *real* public functions (not raw SQL) actually round-trip on a
live local Postgres, exercising every construct the shim cannot auto-translate:
``INSERT OR REPLACE`` upserts, ``lastrowid`` (→ ``RETURNING id``), boolean
columns (PG rejects ``int`` into ``boolean``), and the previously-missing
user-scoped tables/columns.

If a local Postgres is not reachable the whole module is skipped (not failed).
The throwaway PG database is created in a fixture and dropped in teardown even
on failure. ``DATABASE_URL`` and the lazy connection pool are reset around each
test so the SQLite-default rest of the suite is unaffected (the conftest
``_isolate_database_url`` autouse fixture also restores it as a backstop).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# psycopg is a hard project dependency (the PG backend uses it).
psycopg = pytest.importorskip("psycopg")

PG_ADMIN_DSN = os.environ.get("ETL_TEST_PG_ADMIN", "postgresql://localhost:5432/postgres")


def _pg_reachable() -> bool:
    """Return True if the local Postgres admin DSN accepts a connection."""
    try:
        conn = psycopg.connect(PG_ADMIN_DSN, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason=f"local Postgres not reachable at {PG_ADMIN_DSN}",
)


@pytest.fixture
def pg_backend() -> Iterator[str]:
    """Create a throwaway PG DB, point the backend at it, build the schema.

    Yields the seeded owner user's UUID (as a string). Drops the database in
    teardown and restores DATABASE_URL + the lazy pool singleton.
    """
    import kroger_mcp.analytics.database as database
    from kroger_mcp.analytics import pg_database

    db_name = f"pg_backend_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()

    base = PG_ADMIN_DSN.rsplit("/", 1)[0]
    dsn = f"{base}/{db_name}"

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    pg_database.close_pool()
    database.reset_initialization()

    try:
        # Builds the complete PG schema via initialize_pg_database().
        database.ensure_initialized()

        # Seed the owner user the analytics layer resolves to. The default user
        # id is installed by conftest into KROGER_MCP_DEFAULT_USER_ID; reuse it so
        # _resolve_user_id() everywhere lands on a real users row (FK target).
        owner = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
        with pg_database.get_pg_cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, password_hash, display_name) "
                "VALUES (%s, %s, %s, %s)",
                (owner, f"{owner}@example.test", "x", "PG Backend Tester"),
            )
        yield owner
    finally:
        pg_database.close_pool()
        database.reset_initialization()
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url

        admin = psycopg.connect(PG_ADMIN_DSN, autocommit=True)
        try:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            admin.close()


def _seed_product(product_id: str, description: str = "Test Product") -> None:
    """Insert a global product row via the real product-cache upsert path."""
    from kroger_mcp.analytics.purchase_tracker import ensure_product_exists

    ensure_product_exists(product_id, {"description": description, "brand": "TestBrand"})


# ---------------------------------------------------------------------------
# Backend selection sanity
# ---------------------------------------------------------------------------
def test_backend_is_postgresql(pg_backend: str):
    """The fixture really routed get_backend() to Postgres."""
    from kroger_mcp.analytics.database import get_backend

    assert get_backend() == "postgresql"


# ---------------------------------------------------------------------------
# products: cache/upsert path (ensure_product_exists)
# ---------------------------------------------------------------------------
def test_product_cache_upsert(pg_backend: str):
    from kroger_mcp.analytics.database import get_db_connection

    _seed_product("PROD-1", "Heirloom Tomatoes")
    # Idempotent: a second call must not raise or duplicate.
    _seed_product("PROD-1", "Heirloom Tomatoes")

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT description, brand FROM products WHERE product_id = ?",
            ("PROD-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["description"] == "Heirloom Tomatoes"
    assert row["brand"] == "TestBrand"


# ---------------------------------------------------------------------------
# pantry: add / status / set-level (boolean auto_deplete, quantity_on_hand/unit)
# ---------------------------------------------------------------------------
def test_pantry_round_trip(pg_backend: str):
    from kroger_mcp.analytics.pantry import (
        add_to_pantry,
        get_pantry_status,
        update_pantry_level,
    )

    _seed_product("PROD-PANTRY", "Olive Oil")
    res = add_to_pantry(
        "PROD-PANTRY",
        description="Olive Oil",
        level=80,
        auto_deplete=True,
        quantity=2.0,
        unit="bottle",
    )
    assert res["success"] is True

    status = get_pantry_status(apply_depletion=False)
    item = next(i for i in status if i["product_id"] == "PROD-PANTRY")
    assert item["level_percent"] == 80

    upd = update_pantry_level("PROD-PANTRY", 30)
    assert upd["success"] is True
    status2 = get_pantry_status(apply_depletion=False)
    item2 = next(i for i in status2 if i["product_id"] == "PROD-PANTRY")
    assert item2["level_percent"] == 30


# ---------------------------------------------------------------------------
# price history: record_price_observations + read (boolean on_sale)
# ---------------------------------------------------------------------------
def test_price_history_record_and_read(pg_backend: str):
    from kroger_mcp.analytics.deals import (
        get_price_statistics,
        record_price_observations,
    )

    record_price_observations(
        [
            {
                "product_id": "PROD-PRICE",
                "regular_price": 5.00,
                "sale_price": 3.50,
                "location_id": "03400014",
                "source": "search",
            }
        ]
    )
    stats = get_price_statistics("PROD-PRICE", days=30, location_id="03400014")
    assert stats.get("has_data") is True
    assert stats.get("observations_count", 0) >= 1
    # NUMERIC→Decimal coercion: the recommendation math (float * 1.05) ran.
    assert "recommendation" in stats


# ---------------------------------------------------------------------------
# purchase_tracker: record a cart-add event (lastrowid → RETURNING id)
# ---------------------------------------------------------------------------
def test_purchase_event_lastrowid(pg_backend: str):
    from kroger_mcp.analytics.purchase_tracker import record_cart_add

    _seed_product("PROD-EVENT", "Brown Rice")
    event_id = record_cart_add("PROD-EVENT", quantity=2, modality="PICKUP")
    assert isinstance(event_id, int)
    assert event_id > 0


# ---------------------------------------------------------------------------
# meal plan: create + assign (INSERT OR REPLACE on meal_entries) + get
# ---------------------------------------------------------------------------
def test_meal_plan_create_assign_get(pg_backend: str):
    from kroger_mcp.analytics import meal_planning
    from kroger_mcp.analytics.meal_planning import (
        create_meal_plan,
        get_meal_plan,
    )

    created = create_meal_plan(
        name="PG Week",
        start_date="2026-06-15",
        plan_type="weekly",
        is_template=False,
    )
    assert created["success"] is True, created
    plan_id = created["plan_id"]

    # assign_meal exercises INSERT OR REPLACE INTO meal_entries (with user_id).
    # Stub get_recipe so we don't depend on the JSON recipe store.
    real_get_recipe = meal_planning.get_recipe
    meal_planning.get_recipe = lambda rid: {"name": "Test Recipe", "servings": 4}
    try:
        assigned = meal_planning.assign_meal(
            plan_id=plan_id,
            recipe_id="recipe-xyz",
            meal_date="2026-06-16",
            meal_slot="dinner",
        )
        assert assigned["success"] is True, assigned

        # Re-assign the same slot — INSERT OR REPLACE / ON CONFLICT must update.
        reassigned = meal_planning.assign_meal(
            plan_id=plan_id,
            recipe_id="recipe-abc",
            meal_date="2026-06-16",
            meal_slot="dinner",
        )
        assert reassigned["success"] is True, reassigned
    finally:
        meal_planning.get_recipe = real_get_recipe

    plan = get_meal_plan(plan_id, include_recipe_details=False)
    # The upsert replaced the slot rather than duplicating it: exactly one entry,
    # carrying the second recipe_id. Proves ON CONFLICT DO UPDATE works on PG.
    assert plan.get("meal_count") == 1, plan
    summary = plan.get("recipe_summary") or []
    assert any(r.get("recipe_id") == "recipe-abc" for r in summary), plan


# ---------------------------------------------------------------------------
# favorites: create list + add item
# ---------------------------------------------------------------------------
def test_favorites_create_and_add(pg_backend: str):
    from kroger_mcp.analytics.favorites import (
        add_to_list,
        create_list,
        get_list_items,
    )

    _seed_product("PROD-FAV", "Quinoa")
    created = create_list(name="PG Favorites", list_type="custom")
    assert created["success"] is True, created
    list_id = created["list_id"]

    added = add_to_list(
        list_id=list_id,
        product_id="PROD-FAV",
        description="Quinoa",
        brand="TestBrand",
        default_quantity=2,
    )
    assert added["success"] is True, added

    items = get_list_items(list_id=list_id, include_pantry_status=False)
    product_ids = {i["product_id"] for i in items.get("items", [])}
    assert "PROD-FAV" in product_ids


# ---------------------------------------------------------------------------
# safety: approve + block a product (boolean auto_blocked)
# ---------------------------------------------------------------------------
def test_safety_approve_and_block(pg_backend: str):
    from kroger_mcp.analytics.safety import (
        add_to_blocked_list,
        add_to_safe_list,
        get_all_blocked_product_ids,
        get_all_safe_product_ids,
    )

    _seed_product("PROD-SAFE", "Plain Yogurt")
    _seed_product("PROD-BLOCK", "Soda")

    sres = add_to_safe_list("PROD-SAFE", description="Plain Yogurt", reason="clean")
    assert sres["success"] is True
    assert "PROD-SAFE" in get_all_safe_product_ids()

    bres = add_to_blocked_list("PROD-BLOCK", description="Soda", reason="HFCS", auto_blocked=True)
    assert bres["success"] is True
    assert "PROD-BLOCK" in get_all_blocked_product_ids()


# ---------------------------------------------------------------------------
# consent: set + get (user_settings ON CONFLICT upsert)
# ---------------------------------------------------------------------------
def test_consent_set_and_get(pg_backend: str):
    from kroger_mcp.analytics.consent import get_consent, set_consent

    state = set_consent({"price_observations": True})
    assert state["decided"] is True
    assert state["categories"]["price_observations"]["enabled"] is True

    fetched = get_consent()
    assert fetched["decided"] is True
    assert fetched["categories"]["price_observations"]["enabled"] is True
    # An untouched category stays opt-out.
    assert fetched["categories"]["purchase_patterns"]["enabled"] is False


# ---------------------------------------------------------------------------
# pending gap: create + list (lastrowid → RETURNING id in pantry.create_pending_gap)
# ---------------------------------------------------------------------------
def test_pending_gap_lastrowid(pg_backend: str):
    from kroger_mcp.analytics.pantry import create_pending_gap, list_pending_gaps

    _seed_product("PROD-GAP", "Canned Tomatoes")
    gap_id = create_pending_gap(
        product_id="PROD-GAP",
        needed_quantity=2.0,
        ordered_quantity=1.0,
        unit="can",
        recipe_name="Marinara",
    )
    assert isinstance(gap_id, int)
    assert gap_id > 0

    gaps = list_pending_gaps()
    assert any(g.get("id") == gap_id for g in gaps)


# ---------------------------------------------------------------------------
# cart + shopping list: per-user DB-backed save/load (INSERT OR REPLACE)
# ---------------------------------------------------------------------------
def test_cart_save_and_load(pg_backend: str):
    from kroger_mcp.tools.cart_tools import _load_cart_data, _save_cart_data

    _save_cart_data(
        {
            "current_cart": [
                {
                    "product_id": "PROD-CART",
                    "description": "Eggs",
                    "quantity": 1,
                    "modality": "PICKUP",
                }
            ]
        }
    )
    loaded = _load_cart_data()
    pids = {i["product_id"] for i in loaded["current_cart"]}
    assert "PROD-CART" in pids


def test_shopping_list_save_and_load(pg_backend: str):
    from kroger_mcp.tools.shopping_list_tools import (
        _load_shopping_list,
        _save_shopping_list,
    )

    _save_shopping_list(
        {
            "items": [
                {
                    "product_id": "PROD-LIST",
                    "name": "Spinach",
                    "quantity": 2.0,
                    "unit": "bunch",
                }
            ]
        }
    )
    loaded = _load_shopping_list()
    names = {i["name"] for i in loaded["items"]}
    assert "Spinach" in names


# ---------------------------------------------------------------------------
# statistics aggregate: one read over product_statistics-derived predictions
# ---------------------------------------------------------------------------
def test_statistics_aggregate_query(pg_backend: str):
    """A representative aggregate query runs without dialect error on PG."""
    from kroger_mcp.analytics.database import get_db_connection

    _seed_product("PROD-STAT", "Bananas")
    conn = get_db_connection()
    try:
        # Aggregate across purchase_events — boolean/strftime-free, pure SQL agg.
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS q "
            "FROM purchase_events WHERE product_id = ?",
            ("PROD-STAT",),
        ).fetchone()
    finally:
        conn.close()
    assert row["n"] == 0
    assert row["q"] == 0


def _seed_second_user(user_id: str) -> None:
    """Insert a second users row so user-scoping can be observed on PG."""
    from kroger_mcp.analytics import pg_database

    with pg_database.get_pg_cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, email, password_hash, display_name) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (user_id, f"{user_id}@example.test", "x", "Second User"),
        )


# ---------------------------------------------------------------------------
# deal_watchlist: user-scoped upsert (ON CONFLICT(user_id, product_id)) on PG.
# Was global (user_id NOT NULL + UNIQUE(user_id, product_id) made it fail before).
# ---------------------------------------------------------------------------
def test_deal_watchlist_user_scoped_on_pg(pg_backend: str):
    from kroger_mcp.analytics.database import get_db_connection

    owner = pg_backend
    user_b = "99999999-8888-7777-6666-555555555555"
    _seed_second_user(user_b)
    _seed_product("PROD-WATCH", "Olive Oil")

    sql = (
        "INSERT INTO deal_watchlist (user_id, product_id, description, priority) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, product_id) DO UPDATE SET priority = excluded.priority"
    )
    conn = get_db_connection()
    try:
        conn.execute(sql, (owner, "PROD-WATCH", "A watch", 1))
        conn.execute(sql, (user_b, "PROD-WATCH", "B watch", 3))
        conn.commit()
        # Both users watch the same product — composite UNIQUE permits it on PG.
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM deal_watchlist WHERE product_id = ?",
            ("PROD-WATCH",),
        ).fetchone()["n"]
        # Upsert A's row only.
        conn.execute(sql, (owner, "PROD-WATCH", "A again", 2))
        conn.commit()
        a_pri = conn.execute(
            "SELECT priority FROM deal_watchlist WHERE user_id = ? AND product_id = ?",
            (owner, "PROD-WATCH"),
        ).fetchone()["priority"]
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM deal_watchlist WHERE product_id = ?",
            ("PROD-WATCH",),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert total == 2
    assert a_pri == 2
    assert after == 2  # upsert updated, did not duplicate


# ---------------------------------------------------------------------------
# seasonal_patterns: user-scoped write (ON CONFLICT(user_id, product_id, month) +
# bool is_peak_period) and read (boolean WHERE + user filter) on PG.
# ---------------------------------------------------------------------------
def test_seasonal_patterns_user_scoped_on_pg(pg_backend: str):
    import datetime as _dt

    from kroger_mcp.analytics.database import get_db_connection
    from kroger_mcp.analytics.seasonal import (
        get_upcoming_seasonal_items,
        update_seasonal_patterns,
    )

    owner = pg_backend
    _seed_product("PROD-SEASON", "Pumpkin")

    # Seed order_placed events so update_seasonal_patterns produces rows, then run
    # the real writer — proves the upsert + bool() write path runs on PG.
    conn = get_db_connection()
    try:
        for month in (1, 6, 11):
            conn.execute(
                "INSERT INTO purchase_events (product_id, quantity, event_type, "
                "event_date, event_timestamp) VALUES (?, ?, 'order_placed', ?, ?)",
                ("PROD-SEASON", 2, f"2025-{month:02d}-15", f"2025-{month:02d}-15T10:00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    result = update_seasonal_patterns("PROD-SEASON", user_id=owner)
    assert result["product_id"] == "PROD-SEASON"

    conn = get_db_connection()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM seasonal_patterns "
            "WHERE user_id = ? AND product_id = ?",
            (owner, "PROD-SEASON"),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 12  # one row per month, all owned by `owner`

    # Read path: force a peak in the current month, confirm the boolean WHERE +
    # user filter work on PG and isolate by user.
    this_month = _dt.datetime.now().month
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE seasonal_patterns SET is_peak_period = TRUE "
            "WHERE user_id = ? AND product_id = ? AND month = ?",
            (owner, "PROD-SEASON", this_month),
        )
        conn.commit()
    finally:
        conn.close()

    mine = get_upcoming_seasonal_items(days_ahead=2, user_id=owner)
    assert any(i["product_id"] == "PROD-SEASON" for i in mine)
    others = get_upcoming_seasonal_items(days_ahead=2, user_id="00000000-0000-0000-0000-000000000000")
    assert not any(i["product_id"] == "PROD-SEASON" for i in others)
