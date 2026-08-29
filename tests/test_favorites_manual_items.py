"""Tests for favorites items that have no Kroger product behind them.

A "manual" favorite is something Kroger doesn't sell — a farmers-market find, a
home-grown herb, a specialty butcher cut. `favorite_list_items.product_id` is
NOT NULL and half the primary key, so a manual row carries a synthetic
`manual:<uuid>` id plus an `is_manual` flag instead of a real UPC.

These are the specs for that feature end to end: it stores, it survives a
round-trip through every read path, and — the part that actually matters — it is
never sent to the Kroger cart but is never silently dropped either. It surfaces
as MANUAL PURCHASE in the order preview and as an unlinked `manual_purchase` row
on the shopping list, the same shape recipe overrides already use.
"""

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_cursor,
    reset_initialization,
)
from kroger_mcp.analytics.favorites import (
    MANUAL_ID_PREFIX,
    add_to_list,
    bulk_add_to_list,
    get_list_items,
    get_low_stock_items,
)
from kroger_mcp.tools.cart_tools import register_tools as register_cart_tools
from kroger_mcp.tools.favorites_tools import register_tools
from kroger_mcp.tools.shopping_list_tools import _load_shopping_list
from kroger_mcp.web.routes.api.favorites import add_list_to_shopping_list
from kroger_mcp.web.routes.api.products import AddToCartBody, add_product_to_cart

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
LIST_ID = "TESTLIST_manual"
LIST_ID_B = "TESTLIST_manual_dest"
LINKED_PID = "MANUALTEST_LINKED_1"
REASON = "Farmers market only"


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id}))


def _seed():
    with get_db_cursor() as cursor:
        for list_id, name in (
            (LIST_ID, "Manual Items List"),
            (LIST_ID_B, "Manual Items Destination"),
            # The literal 'default' list is what makes get_list_items take its
            # cross-list aggregate (GROUP BY) branch — a separate query shape
            # that has to expose the manual columns too.
            ("default", "All Favorites"),
        ):
            cursor.execute(
                "INSERT OR IGNORE INTO favorite_lists (id, name, user_id) VALUES (?, ?, ?)",
                (list_id, name, USER),
            )


def _cleanup():
    with get_db_cursor() as cursor:
        for list_id in (LIST_ID, LIST_ID_B, "default"):
            cursor.execute("DELETE FROM favorite_list_items WHERE list_id = ?", (list_id,))
            cursor.execute("DELETE FROM favorite_lists WHERE id = ?", (list_id,))
        cursor.execute("DELETE FROM user_shopping_lists WHERE user_id = ?", (USER,))


@pytest.fixture(scope="function")
def clean_db(tmp_path, monkeypatch):
    """Point analytics DB access at a throwaway file, then reset it.

    Same isolation pattern as test_favorites_quantity_api.py's clean_db.
    """
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "favorites_manual_test.db"))
    reset_initialization()
    ensure_initialized()
    _cleanup()
    _seed()
    yield
    _cleanup()
    reset_initialization()


# --- MCP tool plumbing -----------------------------------------------------

# Every `favorites` tool parameter with its declared default. The tool's real
# defaults are pydantic FieldInfo objects, which are only resolved by a live
# MCP server — calling the raw function without these would hand the impl
# FieldInfo instances (e.g. a truthy `items`, sending add_item down the bulk
# path). Passing the full set keeps each test's overrides honest.
_TOOL_DEFAULTS: dict[str, Any] = {
    "name": None,
    "description": None,
    "list_type": "custom",
    "reorder_weeks": None,
    "list_id": "default",
    "new_name": None,
    "new_description": None,
    "product_id": None,
    "product_ids": None,
    "brand": None,
    "default_quantity": 1,
    "preferred_modality": "PICKUP",
    "notes": None,
    "items": None,
    "manual": False,
    "override_reason": None,
    "min_stock_percent": None,
    "min_stock_quantity": None,
    "current_stock_quantity": None,
    "typical_gap_days": None,
    "include_pantry_status": True,
    "sort_by": "description",
    "skip_if_stocked": True,
    "pantry_threshold": 30,
    "modality": None,
    "min_purchases": 3,
    "min_frequency_score": 0.5,
    "limit": 10,
    "confirm": False,
    "confirm_unsafe": False,
    "ctx": None,
}


# Same treatment for the `cart` tool, used to prove a manual id can't be
# ordered through the generic cart-add path.
_CART_DEFAULTS: dict[str, Any] = {
    "product_id": None,
    "quantity": 1,
    "modality": "PICKUP",
    "items": None,
    "preview_only": True,
    "confirm_unsafe": False,
    "order_notes": None,
    "limit": 10,
    "product_ids": None,
    "pantry_threshold": 30,
    "ctx": None,
}


def _register(register_func, tool_name: str):
    """Register a tool module against a stub MCP and return the named closure."""
    captured: dict[str, Any] = {}

    def capture_tool(func):
        captured[func.__name__] = func
        return func

    register_func(SimpleNamespace(tool=lambda: capture_tool))
    return captured[tool_name]


def _call_tool(action: str, **overrides) -> dict[str, Any]:
    kwargs = {**_TOOL_DEFAULTS, **overrides}
    return asyncio.run(_register(register_tools, "favorites")(action=action, **kwargs))


def _call_cart_tool(action: str, **overrides) -> dict[str, Any]:
    kwargs = {**_CART_DEFAULTS, **overrides}
    return asyncio.run(_register(register_cart_tools, "cart")(action=action, **kwargs))


def _add_manual(
    list_id: str = LIST_ID,
    description: str = "Backyard basil",
    override_reason: str | None = REASON,
    **kwargs,
) -> dict[str, Any]:
    return add_to_list(
        list_id=list_id,
        product_id=None,
        description=description,
        manual=True,
        override_reason=override_reason,
        user_id=USER,
        **kwargs,
    )


def _add_linked(list_id: str = LIST_ID, product_id: str = LINKED_PID) -> dict[str, Any]:
    return add_to_list(
        list_id=list_id,
        product_id=product_id,
        description="Olive Oil",
        user_id=USER,
    )


def _stored_is_manual(list_id: str, product_id: str) -> bool:
    with get_db_cursor() as cursor:
        row = cursor.execute(
            "SELECT is_manual FROM favorite_list_items WHERE list_id = ? AND product_id = ?",
            (list_id, product_id),
        ).fetchone()
    return bool(row["is_manual"])


# --- Storage ---------------------------------------------------------------


def test_add_item_without_product_id(clean_db):
    """Spec: a manual item is stored under a synthetic id and flagged is_manual."""
    result = _add_manual()

    assert result["success"] is True
    assert result["product_id"].startswith(MANUAL_ID_PREFIX)
    assert result["is_manual"] is True
    assert result["override_reason"] == REASON
    assert _stored_is_manual(LIST_ID, result["product_id"]) is True


def test_add_item_still_requires_product_or_manual(clean_db):
    """Spec: dropping product_id without saying `manual` is still an error.

    Otherwise a failed product lookup would quietly become a manual item.
    """
    result = _call_tool(
        "add_item", list_id=LIST_ID, description="Mystery item", product_id=None, manual=False
    )

    assert result["success"] is False
    assert "manual=True" in result["error"]


def test_manual_item_without_reason_is_accepted(clean_db):
    """Spec: override_reason is optional for favorites (unlike recipe overrides)."""
    result = _add_manual(description="Grandma's jam", override_reason=None)

    assert result["success"] is True
    assert result["is_manual"] is True
    assert result["override_reason"] is None


def test_bulk_add_mixes_manual_and_linked(clean_db):
    """Spec: one bulk_add call can carry both linked and manual items."""
    result = bulk_add_to_list(
        list_id=LIST_ID,
        items=[
            {"product_id": LINKED_PID, "description": "Olive Oil"},
            {"description": "Heirloom tomatoes", "manual": True, "override_reason": REASON},
        ],
        user_id=USER,
    )

    assert result["success"] is True
    assert result["added_count"] == 2

    by_description = {item["description"]: item for item in result["added"]}
    assert by_description["Olive Oil"]["is_manual"] is False
    tomatoes = by_description["Heirloom tomatoes"]
    assert tomatoes["is_manual"] is True
    assert tomatoes["product_id"].startswith(MANUAL_ID_PREFIX)


# --- Read paths ------------------------------------------------------------


def test_get_items_exposes_manual_fields(clean_db):
    """Spec: both get_list_items query shapes expose is_manual/override_reason.

    The per-list branch selects the columns directly; the 'default' branch
    aggregates across lists with GROUP BY and has to carry them through too.
    """
    manual_pid = _add_manual()["product_id"]
    _add_linked()

    per_list = get_list_items(LIST_ID, user_id=USER)
    assert per_list["success"] is True
    per_list_by_id = {item["product_id"]: item for item in per_list["items"]}
    assert per_list_by_id[manual_pid]["is_manual"] is True
    assert per_list_by_id[manual_pid]["override_reason"] == REASON
    assert per_list_by_id[LINKED_PID]["is_manual"] is False

    aggregate = get_list_items("default", user_id=USER)
    assert aggregate["success"] is True
    aggregate_by_id = {item["product_id"]: item for item in aggregate["items"]}
    assert aggregate_by_id[manual_pid]["is_manual"] is True
    assert aggregate_by_id[manual_pid]["override_reason"] == REASON
    assert aggregate_by_id[LINKED_PID]["is_manual"] is False


def test_move_preserves_manual_flag(clean_db):
    """Spec: moving a manual item between lists must not relink it as a product.

    "Move to List" re-posts the item's stored product_id to the new list. If the
    caller forgets to resend `manual`, the synthetic id's prefix is the backstop
    — otherwise the moved item would look like a real UPC and reach the cart.
    """
    manual_pid = _add_manual()["product_id"]

    moved = add_to_list(
        list_id=LIST_ID_B,
        product_id=manual_pid,
        description="Backyard basil",
        manual=False,
        user_id=USER,
    )

    assert moved["success"] is True
    assert moved["is_manual"] is True
    assert _stored_is_manual(LIST_ID_B, manual_pid) is True


def test_manual_item_appears_in_low_stock(clean_db):
    """Spec: manual stock levels still work — thresholds are local to the row.

    Purchase-history features skip manual items (no Kroger history exists), but
    the user-managed count is the whole point of tracking a manual item here.
    """
    manual_pid = _add_manual(min_stock_quantity=2, current_stock_quantity=0)["product_id"]

    result = get_low_stock_items(LIST_ID, user_id=USER)

    assert result["success"] is True
    low = {item["product_id"]: item for item in result["low_stock_items"]}
    assert manual_pid in low
    assert low[manual_pid]["is_manual"] is True
    assert low[manual_pid]["below_min_quantity"] is True


# --- Downstream: never carted, never dropped -------------------------------


def test_order_preview_splits_manual_items(clean_db):
    """Spec: `order` reports manual items separately instead of carting them."""
    manual_pid = _add_manual()["product_id"]
    _add_linked()

    result = _call_tool("order", list_id=LIST_ID, confirm=False)

    assert result["success"] is True
    preview = result["preview"]

    ordered_ids = {item["product_id"] for item in preview["items_to_order"]}
    assert LINKED_PID in ordered_ids
    assert manual_pid not in ordered_ids

    assert preview["manual_count"] == 1
    manual_row = preview["manual_purchase"][0]
    assert manual_row["product_id"] == manual_pid
    assert manual_row["override_reason"] == REASON
    assert manual_row["action"] == "MANUAL"
    assert "MANUAL PURCHASE" in result["next_step"]


def test_cart_add_rejects_a_manual_id(clean_db):
    """Spec: the generic cart-add paths refuse a synthetic manual id.

    The favorites UI hides "+ Cart" for manual items, but that invariant can't
    live only in the browser — both the web route and the MCP `cart` tool take
    a caller-supplied product_id straight into the Kroger `upc` field.
    """
    manual_pid = _add_manual()["product_id"]

    web_response = asyncio.run(
        add_product_to_cart(manual_pid, AddToCartBody(quantity=1), _request(USER))
    )
    assert web_response.status_code == 400
    assert b"not sold at Kroger" in web_response.body

    tool_result = _call_cart_tool("add", product_id=manual_pid, preview_only=False)
    assert tool_result["success"] is False
    # The phrase pins it to the guard — an auth/API failure also returns
    # success=False and would otherwise make this test pass for free.
    assert "not sold at Kroger" in tool_result["error"]
    assert manual_pid in tool_result["error"]

    # Batch mode must fail before ordering ANY item, not partway through.
    batch_result = _call_cart_tool(
        "add",
        items=[
            {"product_id": LINKED_PID, "quantity": 1},
            {"product_id": manual_pid, "quantity": 1},
        ],
        preview_only=False,
    )
    assert batch_result["success"] is False
    assert "not sold at Kroger" in batch_result["error"]
    assert manual_pid in batch_result["error"]


def test_add_to_shopping_list_marks_manual_items(clean_db):
    """Spec: manual favorites reach the shopping list unlinked and flagged.

    `product_id: None` + `manual_purchase: True` is the shape the cart-send path
    already understands from recipe overrides. Asserted after a save/load
    round-trip, because the flag is only useful if it survives persistence.
    """
    _add_manual()
    _add_linked()

    response = asyncio.run(add_list_to_shopping_list(LIST_ID, _request(USER)))

    assert response["success"] is True
    assert response["items_manual"] == 1

    reloaded = {item["name"]: item for item in _load_shopping_list(user_id=USER)["items"]}

    basil = reloaded["Backyard basil"]
    assert basil["product_id"] is None
    assert basil["manual_purchase"] is True
    assert basil["notes"] == REASON

    olive_oil = reloaded["Olive Oil"]
    assert olive_oil["product_id"] == LINKED_PID
    assert olive_oil["manual_purchase"] is False
