"""Oracle for plans/mcp-web-parity.md: every web-app feature (except cart adds)
has an MCP action.

Each test drives the real MCP surface through an in-process
`fastmcp.Client(create_server())`, so parameter defaults, the action Literal and
the dispatcher are all exercised exactly as an agent would hit them. Where an
action's backend needs data that is expensive to seed (recipes, cooked meals),
the analytics function is monkeypatched on its module and the test asserts the
tool forwards the right arguments, including the caller's user_id.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from _pg_support import skip_on_pg

from kroger_mcp.analytics import meal_planning
from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.analytics.pantry import add_to_pantry, get_pantry_item
from kroger_mcp.auth.dependencies import default_user_id
from kroger_mcp.tools.shopping_list_tools import _load_shopping_list, _save_shopping_list
from kroger_mcp.web import risk_policy

pytestmark = skip_on_pg

OTHER_USER = "00000000-0000-0000-0000-0000000000b2"


@pytest.fixture
def user(tmp_path, monkeypatch):
    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "mcp_parity.db"))
    monkeypatch.delenv("KROGER_MCP_USER_ID", raising=False)
    reset_initialization()
    ensure_initialized()
    yield default_user_id()
    reset_initialization()


def call(tool: str, **args: Any) -> dict[str, Any]:
    """Call one MCP tool action through a real in-process client."""
    from fastmcp import Client

    from kroger_mcp.server import create_server

    async def run() -> dict[str, Any]:
        async with Client(create_server()) as client:
            result = await client.call_tool(tool, args)
            # TokenSlimMiddleware sends the payload once, as compact JSON text
            # (no structuredContent copy), so read it the way any client does.
            return dict(json.loads(result.content[0].text))

    return asyncio.run(run())


def actions_of(tool: str) -> set[str]:
    from fastmcp import Client

    from kroger_mcp.server import create_server

    async def run() -> set[str]:
        async with Client(create_server()) as client:
            for t in await client.list_tools():
                if t.name == tool:
                    return set(t.inputSchema["properties"]["action"]["enum"])
        raise AssertionError(f"tool {tool!r} not registered")

    return asyncio.run(run())


# --- surface ----------------------------------------------------------------

EXPECTED_NEW_ACTIONS = {
    "shopping_list": {"add_item", "clear", "mark_empty"},
    "pantry": {"get_history", "clear"},
    "deals": {"get_watchlist", "remove_from_watchlist"},
    "notifications": {"list", "mark_seen", "dismiss"},
    "recipes": {"mark_cooked", "undo_cooked", "cook_preview", "get_step_times"},
    "meal_plan": {
        "list_templates",
        "create_from_template",
        "set_template",
        "confirm_pending",
        "cook_preview",
        "schedule_recipe",
    },
    "favorites": {"add_list_to_shopping_list", "add_snacks_to_list"},
    "products": {"suggest_for_ingredient"},
    "info": {"set_include_spices_by_default", "set_pantry_deduction_mode"},
}


@pytest.mark.parametrize("tool", sorted(EXPECTED_NEW_ACTIONS))
def test_new_actions_are_registered(tool):
    assert EXPECTED_NEW_ACTIONS[tool] <= actions_of(tool)


# --- shopping_list ----------------------------------------------------------


def test_shopping_list_add_item_linked_and_manual(user):
    linked = call("shopping_list", action="add_item", name="Milk", product_id="p-milk", quantity=2)
    manual = call("shopping_list", action="add_item", name="Gochujang", source="H Mart")
    assert linked["success"] and manual["success"]
    assert linked["item_id"] and manual["item_id"]
    items = {i["name"]: i for i in _load_shopping_list(user_id=user)["items"]}
    assert items["Milk"]["product_id"] == "p-milk"
    assert items["Milk"]["quantity"] == 2
    assert items["Milk"]["manual_purchase"] is False
    assert items["Gochujang"]["product_id"] is None
    assert items["Gochujang"]["manual_purchase"] is True
    assert items["Gochujang"]["source"]


def test_shopping_list_add_item_requires_name(user):
    result = call("shopping_list", action="add_item")
    assert result["success"] is False


def test_shopping_list_clear_only_touches_caller(user):
    _save_shopping_list({"items": [{"id": "a", "product_id": "x", "name": "X", "quantity": 1}]}, user_id=user)
    _save_shopping_list({"items": [{"id": "b", "product_id": "y", "name": "Y", "quantity": 1}]}, user_id=OTHER_USER)
    result = call("shopping_list", action="clear")
    assert result["success"] and result["cleared"] == 1
    assert _load_shopping_list(user_id=user)["items"] == []
    assert len(_load_shopping_list(user_id=OTHER_USER)["items"]) == 1


def test_shopping_list_mark_empty(user):
    add_to_pantry(product_id="p-eggs", description="Eggs", level=60, user_id=user)
    _save_shopping_list(
        {"items": [
            {"id": "a", "product_id": "p-eggs", "name": "Eggs", "quantity": 1},
            {"id": "b", "product_id": None, "name": "Herbs", "quantity": 1},
        ]},
        user_id=user,
    )
    result = call("shopping_list", action="mark_empty")
    assert result["success"] and result["emptied"] == 1 and result["skipped"] == 1
    assert get_pantry_item("p-eggs", user_id=user)["level_percent"] == 0


def test_web_mark_empty_import_still_works():
    from kroger_mcp.web.routes.api.shopping_list import _mark_list_items_empty

    assert callable(_mark_list_items_empty)


# --- pantry -----------------------------------------------------------------


def test_pantry_clear_requires_confirm_and_is_user_scoped(user):
    add_to_pantry(product_id="p-1", description="One", level=50, user_id=user)
    add_to_pantry(product_id="p-2", description="Two", level=50, user_id=user)
    add_to_pantry(product_id="p-3", description="Theirs", level=50, user_id=OTHER_USER)

    refused = call("pantry", action="clear")
    assert refused["success"] is False
    assert get_pantry_item("p-1", user_id=user) is not None

    done = call("pantry", action="clear", confirm=True)
    assert done["success"] and done["deleted"] == 2
    assert get_pantry_item("p-1", user_id=user) is None
    assert get_pantry_item("p-3", user_id=OTHER_USER) is not None


def test_clear_pantry_helper_is_shared():
    from kroger_mcp.analytics.pantry import clear_pantry

    assert callable(clear_pantry)


def test_pantry_get_history_forwards_user(user, monkeypatch):
    seen: dict[str, Any] = {}

    def fake(product_id, days=30, *, user_id):
        seen.update(product_id=product_id, days=days, user_id=user_id)
        return [{"event_type": "purchase"}]

    monkeypatch.setattr("kroger_mcp.analytics.pantry.get_usage_history", fake)
    result = call("pantry", action="get_history", product_id="p-9", days=14)
    assert result["success"] and result["events"] == [{"event_type": "purchase"}]
    assert seen == {"product_id": "p-9", "days": 14, "user_id": user}


# --- deals watchlist --------------------------------------------------------


def _watch(user_id: str, product_id: str) -> None:
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO products (product_id, description) VALUES (?, ?)",
            (product_id, product_id),
        )
        cursor.execute(
            "INSERT INTO deal_watchlist (user_id, product_id, description, added_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, product_id, product_id, datetime.now().isoformat()),
        )


def test_deals_watchlist_get_and_remove(user):
    _watch(user, "w-1")
    _watch(user, "w-2")
    _watch(OTHER_USER, "w-3")
    listed = call("deals", action="get_watchlist")
    assert listed["success"]
    assert {r["product_id"] for r in listed["items"]} == {"w-1", "w-2"}

    removed = call("deals", action="remove_from_watchlist", product_id="w-1")
    assert removed["success"]
    assert {r["product_id"] for r in call("deals", action="get_watchlist")["items"]} == {"w-2"}

    # Another user's row can't be removed through this caller.
    call("deals", action="remove_from_watchlist", product_id="w-3")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM deal_watchlist WHERE user_id = ?", (OTHER_USER,))
        assert cursor.fetchone()["n"] == 1


# --- notifications ----------------------------------------------------------


def _alert(user_id: str, product_id: str) -> int:
    with get_db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO favorite_sale_alerts (user_id, product_id, list_id, description, "
            "regular_price, sale_price, savings_percent, default_quantity, "
            "preferred_modality, created_at) VALUES (?, ?, 'l', ?, 4.0, 3.0, 25, 1, 'PICKUP', ?)",
            (user_id, product_id, product_id, datetime.now().isoformat()),
        )
        cursor.execute(
            "SELECT id FROM favorite_sale_alerts WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        )
        return int(cursor.fetchone()["id"])


def test_notifications_list_mark_seen_dismiss(user):
    a1 = _alert(user, "n-1")
    a2 = _alert(user, "n-2")
    theirs = _alert(OTHER_USER, "n-3")

    listed = call("notifications", action="list")
    assert listed["success"]
    for key in ("alerts", "pending_meals", "pantry_alerts", "needs_plan", "unseen"):
        assert key in listed
    assert {a["product_id"] for a in listed["alerts"]} == {"n-1", "n-2"}
    assert listed["unseen"] >= 2

    assert call("notifications", action="mark_seen")["seen"] == 2

    assert call("notifications", action="dismiss", alert_id=a1)["success"]
    acted = call("notifications", action="dismiss", alert_id=a2, acted=True)
    assert acted["success"]
    assert call("notifications", action="list")["alerts"] == []
    with get_db_cursor() as cursor:
        cursor.execute("SELECT acted FROM favorite_sale_alerts WHERE id = ?", (a2,))
        assert cursor.fetchone()["acted"] in (1, True)

    # Cross-user dismiss is a not-found, never a write.
    assert call("notifications", action="dismiss", alert_id=theirs)["success"] is False


# --- recipes ----------------------------------------------------------------


def _capture(monkeypatch, target: str, ret: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return ret

    monkeypatch.setattr(target, fake)
    return seen


def test_recipes_cook_preview_mark_and_undo(user, monkeypatch):
    prev = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.preview_recipe_cook",
                    {"success": True, "ingredients": []})
    assert call("recipes", action="cook_preview", recipe_id="r1", servings=3)["success"]
    assert prev["kwargs"]["user_id"] == user
    assert "r1" in prev["args"] or prev["kwargs"].get("recipe_id") == "r1"

    cook = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.cook_recipe_adhoc",
                    {"success": True, "cook_event_id": "ce-1"})
    out = call("recipes", action="mark_cooked", recipe_id="r1")
    assert out["success"] and out["cook_event_id"] == "ce-1"
    assert cook["kwargs"]["user_id"] == user

    undo = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.undo_recipe_adhoc",
                    {"success": True})
    assert call("recipes", action="undo_cooked", cook_event_id="ce-1")["success"]
    assert undo["kwargs"]["user_id"] == user
    assert "ce-1" in undo["args"] or undo["kwargs"].get("cook_event_id") == "ce-1"


def test_recipes_undo_cooked_requires_event_id(user):
    assert call("recipes", action="undo_cooked")["success"] is False


def test_recipes_get_step_times(user, monkeypatch):
    recipe = {"id": "r1", "name": "Soup", "instructions": "1. Simmer 20 minutes.\n2. Serve."}
    monkeypatch.setattr("kroger_mcp.tools.recipe_tools._find_recipe", lambda rid: recipe if rid == "r1" else None)
    out = call("recipes", action="get_step_times", recipe_id="r1")
    assert out["success"]
    for key in ("times", "totals", "summary"):
        assert key in out
    assert call("recipes", action="get_step_times", recipe_id="missing")["success"] is False


# --- meal_plan --------------------------------------------------------------


def test_meal_plan_templates_roundtrip(user):
    plan = meal_planning.create_meal_plan(name="Base week", start_date="2030-01-07", user_id=user)
    plan_id = plan["plan_id"]
    assert call("meal_plan", action="list_templates")["plans"] == []

    marked = call("meal_plan", action="set_template", plan_id=plan_id, is_template=True)
    assert marked["success"]
    templates = call("meal_plan", action="list_templates")["plans"]
    assert [p["id"] for p in templates] == [plan_id]

    copied = call("meal_plan", action="create_from_template", source_plan_id=plan_id,
                  new_name="Copy", new_start_date="2030-02-04")
    assert copied["success"]

    assert call("meal_plan", action="set_template", plan_id=plan_id, is_template=False)["success"]
    assert call("meal_plan", action="list_templates")["plans"] == []


def test_meal_plan_set_template_is_owner_scoped(user):
    theirs = meal_planning.create_meal_plan(name="Theirs", start_date="2030-01-07", user_id=OTHER_USER)
    out = call("meal_plan", action="set_template", plan_id=theirs["plan_id"], is_template=True)
    assert out["success"] is False


def test_meal_plan_confirm_pending_and_cook_preview(user, monkeypatch):
    conf = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.confirm_all_pending_meals",
                    {"success": True, "confirmed": 0})
    assert call("meal_plan", action="confirm_pending")["success"]
    assert user in conf["args"] or conf["kwargs"].get("user_id") == user

    prev = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.preview_meal_cook",
                    {"success": True, "ingredients": []})
    out = call("meal_plan", action="cook_preview", plan_id="p1",
               meal_date="2030-01-07", meal_slot="dinner")
    assert out["success"]
    assert user in prev["args"] or prev["kwargs"].get("user_id") == user


def test_meal_plan_schedule_recipe(user, monkeypatch):
    assigned = _capture(monkeypatch, "kroger_mcp.analytics.meal_planning.assign_meal",
                        {"success": True, "recipe_name": "Soup"})
    when = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    out = call("meal_plan", action="schedule_recipe", recipe_id="r1",
               meal_date=when, meal_slot="dinner")
    assert out["success"] and out["created_plan"] is True and out["plan_id"]
    assert assigned["kwargs"]["user_id"] == user
    assert meal_planning.find_plan_covering_date(when, user_id=user)["id"] == out["plan_id"]

    past = call("meal_plan", action="schedule_recipe", recipe_id="r1",
                meal_date=datetime.now().strftime("%Y-%m-%d"), meal_slot="dinner")
    assert past["success"] is False


# --- favorites --------------------------------------------------------------


def test_favorites_add_list_to_shopping_list(user):
    from kroger_mcp.analytics.favorites import add_to_list

    with get_db_cursor() as cursor:
        cursor.execute("INSERT INTO favorite_lists (id, name, user_id) VALUES ('L1', 'Weekly', ?)", (user,))
    add_to_list(product_id="f-1", description="Bread", list_id="L1", user_id=user)
    add_to_pantry(product_id="f-2", description="Rice", level=90, user_id=user)
    add_to_list(product_id="f-2", description="Rice", list_id="L1", user_id=user)

    out = call("favorites", action="add_list_to_shopping_list", list_id="L1")
    assert out["success"] and out["items_added"] == 1 and out["items_skipped"] == 1
    names = [i["name"] for i in _load_shopping_list(user_id=user)["items"]]
    assert names == ["Bread"]


def test_favorites_add_snacks_to_list_honors_allow_list(user, monkeypatch):
    monkeypatch.setattr(
        "kroger_mcp.analytics.favorites.check_snacks",
        lambda user_id: {"candidates": [
            {"product_id": "s-1", "description": "Chips", "list_id": "SL", "default_quantity": 2},
        ]},
    )
    out = call("favorites", action="add_snacks_to_list", product_ids=["s-1", "forged"])
    assert out["success"] and out["items_added"] == 1
    items = _load_shopping_list(user_id=user)["items"]
    assert [(i["product_id"], i["quantity"]) for i in items] == [("s-1", 2)]


# --- products ---------------------------------------------------------------


def test_products_suggest_for_ingredient(user, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_suggest(user_id, term, limit=6):
        seen.update(user_id=user_id, term=term, limit=limit)
        return [{"product_id": "x", "description": "Garlic", "reason": "usual", "score": 0.9}]

    monkeypatch.setattr("kroger_mcp.analytics.ingredient_links.suggest_products_for_ingredient", fake_suggest)
    out = call("products", action="suggest_for_ingredient", search_term="garlic", limit=3)
    assert out["success"]
    assert out["suggestions"][0]["product_id"] == "x"
    # Null fields are dropped by TokenSlimMiddleware; with no confident match
    # both of these are null, so they read as absent.
    for key in ("canonical_name", "best_guess"):
        assert out.get(key) is None
    assert seen == {"user_id": user, "term": "garlic", "limit": 3}


# --- info -------------------------------------------------------------------


def test_info_spices_and_deduction_mode(user):
    assert call("info", action="set_include_spices_by_default", value=1)["success"]
    assert call("info", action="set_pantry_deduction_mode", mode="confirm")["success"]
    profile = call("info", action="get_preferences")["profile"]
    assert profile["include_spices_by_default"] is True
    assert profile["meal_plan_pantry_deduction_mode"] == "confirm"

    bad = call("info", action="set_pantry_deduction_mode", mode="sometimes")
    assert bad["success"] is False


# --- chat risk tiers for the new actions --------------------------------------


@pytest.mark.parametrize("tool,action,args,tier", [
    ("recipes", "cook_preview", {}, "read_only"),
    ("meal_plan", "cook_preview", {}, "read_only"),
    ("meal_plan", "list_templates", {}, "read_only"),
    ("notifications", "list", {}, "read_only"),
    ("products", "suggest_for_ingredient", {}, "read_only"),
    ("deals", "get_watchlist", {}, "read_only"),
    ("pantry", "get_history", {}, "read_only"),
    ("shopping_list", "clear", {}, "write"),
    ("shopping_list", "add_item", {}, "write"),
    ("recipes", "mark_cooked", {}, "write"),
    ("pantry", "clear", {}, "read_only"),
    ("pantry", "clear", {"confirm": True}, "hard_blocked"),
])
def test_risk_tiers(tool, action, args, tier):
    assert risk_policy.classify(tool, action, args) == tier
