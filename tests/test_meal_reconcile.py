"""Lazy auto-deduction of past meal-plan meals into the pantry.

Critical-path coverage (data integrity): the pantry must reflect what the meal
plan consumed. These tests verify the lazy reconciler auto-deducts strictly-past
meals exactly once, matches typed-name ingredients by description, keeps undo of
a past meal permanent (cook_skipped tombstone), and that shopping-list generation
stops re-buying ingredients for meals already deducted.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _pg_support import skip_on_pg

from kroger_mcp.analytics import meal_planning
from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    reset_initialization,
)
from kroger_mcp.analytics.meal_planning import (
    assign_meal,
    confirm_all_pending_meals,
    create_meal_plan,
    generate_meal_plan_shopping_list,
    list_pending_meals,
    reconcile_past_meals,
    skip_pending_meal,
    undo_meal_cooked,
)
from kroger_mcp.analytics.pantry import add_to_pantry, get_pantry_item
from kroger_mcp.tools.shared import set_meal_plan_pantry_deduction_mode

# Uses SQLite table resets + direct internal calls; the PG suite truncates per test.
pytestmark = skip_on_pg


def _today() -> datetime:
    return datetime.now()


def _date(offset_days: int) -> str:
    return (_today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture
def clean_db():
    reset_initialization()
    ensure_initialized()
    conn = get_db_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "cook_deductions",
            "meal_entries",
            "meal_plans",
            "purchase_events",
            "pantry_items",
            "products",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()
    # Legacy silent-auto-deduct behavior is now opt-in; most tests in this
    # file predate the 'confirm' default and assume automatic reconciliation.
    set_meal_plan_pantry_deduction_mode("automatic")
    yield
    reset_initialization()


def _fake_recipe(ingredients, name="Test Dish", servings=4):
    return {"name": name, "servings": servings, "ingredients": ingredients}


def _seed_plan_with_meal(recipe_id, meal_date, slot="dinner"):
    """A plan spanning ±5 days with one meal assigned at `meal_date`."""
    plan = create_meal_plan("Recon Plan", _date(-5), _date(5), plan_type="custom")
    plan_id = plan["plan_id"] if "plan_id" in plan else plan.get("plan", {}).get("id")
    assign_meal(plan_id, recipe_id, meal_date, slot, servings_override=4)
    return plan_id


def _count_deductions(product_id):
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM cook_deductions WHERE product_id = ?",
            (product_id,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _meal_row(plan_id):
    conn = get_db_connection()
    try:
        return dict(
            conn.execute(
                "SELECT pantry_deducted, cook_skipped, cooked_at FROM meal_entries "
                "WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        )
    finally:
        conn.close()


# ── reconcile ────────────────────────────────────────────────────────────────

def test_reconcile_deducts_past_meal_once(clean_db, monkeypatch):
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    plan_id = _seed_plan_with_meal("R1", _date(-1))

    result = reconcile_past_meals(today=_date(0), mode="automatic")

    assert result["reconciled"] == 1
    assert get_pantry_item("RC_OIL")["level_percent"] < 100
    assert _count_deductions("RC_OIL") == 1
    assert _meal_row(plan_id)["pantry_deducted"] in (1, True)


def test_reconcile_is_idempotent(clean_db, monkeypatch):
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    _seed_plan_with_meal("R1", _date(-1))

    reconcile_past_meals(today=_date(0), mode="automatic")
    level_after_first = get_pantry_item("RC_OIL")["level_percent"]
    second = reconcile_past_meals(today=_date(0), mode="automatic")

    assert second["reconciled"] == 0
    assert get_pantry_item("RC_OIL")["level_percent"] == level_after_first
    assert _count_deductions("RC_OIL") == 1  # no double deduct


def test_reconcile_skips_today_and_future(clean_db, monkeypatch):
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    _seed_plan_with_meal("R1", _date(0))   # today — not strictly past
    _seed_plan_with_meal("R2", _date(2), slot="lunch")

    result = reconcile_past_meals(today=_date(0), mode="automatic")

    assert result["reconciled"] == 0
    assert get_pantry_item("RC_OIL")["level_percent"] == 100


def test_fuzzy_match_deducts_typed_name_ingredient(clean_db, monkeypatch):
    # Ingredient has no product_id; pantry item is "Fresh Garlic" → resolved by name.
    add_to_pantry("RC_GARLIC", "Fresh Garlic", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "garlic", "product_id": None, "quantity": 2}]),
    )
    _seed_plan_with_meal("R1", _date(-1))

    reconcile_past_meals(today=_date(0), mode="automatic")

    assert get_pantry_item("RC_GARLIC")["level_percent"] < 100
    assert _count_deductions("RC_GARLIC") == 1


def test_unmatched_ingredient_is_skipped_not_errored(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "unobtanium spice", "product_id": None, "quantity": 1}]),
    )
    plan_id = _seed_plan_with_meal("R1", _date(-1))

    result = reconcile_past_meals(today=_date(0), mode="automatic")

    assert result["reconciled"] == 1  # meal handled, just nothing to deduct
    assert not result["skipped"]
    assert _meal_row(plan_id)["pantry_deducted"] in (1, True)  # marked, won't retry forever


# ── undo respects the tombstone ──────────────────────────────────────────────

def test_undo_past_meal_sets_tombstone_and_blocks_re_reconcile(clean_db, monkeypatch):
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    past = _date(-1)
    plan_id = _seed_plan_with_meal("R1", past)

    reconcile_past_meals(today=_date(0), mode="automatic")
    undo = undo_meal_cooked(plan_id, past, "dinner")

    assert undo["success"] is True
    assert get_pantry_item("RC_OIL")["level_percent"] == 100  # restored
    row = _meal_row(plan_id)
    assert row["cook_skipped"] in (1, True)
    assert _count_deductions("RC_OIL") == 0  # ledger cleared

    # A later view must NOT silently re-deduct the meal the user undid.
    again = reconcile_past_meals(today=_date(0), mode="automatic")
    assert again["reconciled"] == 0
    assert get_pantry_item("RC_OIL")["level_percent"] == 100


# ── shopping list excludes cooked meals ──────────────────────────────────────

def test_shopping_list_excludes_reconciled_meals(clean_db, monkeypatch):
    add_to_pantry("RC_OIL", "Olive Oil", level=100)

    def _recipe(rid):
        if rid == "PAST":
            return _fake_recipe(
                [{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}], name="Past Dish"
            )
        return _fake_recipe(
            [{"name": "flour", "product_id": "RC_FLOUR", "quantity": 1}], name="Future Dish"
        )

    monkeypatch.setattr(meal_planning, "get_recipe", _recipe)

    plan = create_meal_plan("List Plan", _date(-3), _date(3), plan_type="custom")
    plan_id = plan["plan_id"]
    assign_meal(plan_id, "PAST", _date(-1), "dinner", servings_override=4)
    assign_meal(plan_id, "FUTURE", _date(1), "dinner", servings_override=4)

    result = generate_meal_plan_shopping_list(plan_id=plan_id)

    names = {r["recipe_name"] for r in result["recipes_included"]}
    assert "Future Dish" in names
    assert "Past Dish" not in names  # reconciled + excluded


# ── confirm mode (default) ────────────────────────────────────────────────────

def test_reconcile_confirm_mode_does_not_deduct(clean_db, monkeypatch):
    set_meal_plan_pantry_deduction_mode("confirm")
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    plan_id = _seed_plan_with_meal("R1", _date(-1))

    result = reconcile_past_meals(today=_date(0))

    assert result["reconciled"] == 0
    assert result["pending"] == 1
    assert get_pantry_item("RC_OIL")["level_percent"] == 100  # untouched
    assert _meal_row(plan_id)["pantry_deducted"] in (0, False)

    pending = list_pending_meals(today=_date(0))
    assert len(pending) == 1
    assert pending[0]["plan_id"] == plan_id


def test_confirm_all_pending_meals_deducts_all(clean_db, monkeypatch):
    set_meal_plan_pantry_deduction_mode("confirm")
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    plan_id = _seed_plan_with_meal("R1", _date(-1))
    reconcile_past_meals(today=_date(0))  # confirm mode — leaves it pending

    result = confirm_all_pending_meals(today=_date(0))

    assert result["reconciled"] == 1
    assert get_pantry_item("RC_OIL")["level_percent"] < 100
    assert _meal_row(plan_id)["pantry_deducted"] in (1, True)
    assert list_pending_meals(today=_date(0)) == []


def test_skip_pending_meal_sets_tombstone_without_deduction(clean_db, monkeypatch):
    set_meal_plan_pantry_deduction_mode("confirm")
    add_to_pantry("RC_OIL", "Olive Oil", level=100)
    monkeypatch.setattr(
        meal_planning, "get_recipe",
        lambda rid: _fake_recipe([{"name": "olive oil", "product_id": "RC_OIL", "quantity": 1}]),
    )
    past = _date(-1)
    plan_id = _seed_plan_with_meal("R1", past)

    result = skip_pending_meal(plan_id, past, "dinner")

    assert result["success"] is True
    row = _meal_row(plan_id)
    assert row["cook_skipped"] in (1, True)
    assert row["pantry_deducted"] in (0, False)
    assert get_pantry_item("RC_OIL")["level_percent"] == 100  # untouched
    assert list_pending_meals(today=_date(0)) == []  # tombstoned, no longer pending
