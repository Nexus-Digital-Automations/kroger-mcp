"""Shopping list API endpoints."""

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.manual_sources import (
    group_by_source,
    is_manual_item,
    item_source,
    manual_note,
    stored_source,
)
from kroger_mcp.auth.dependencies import current_user_id
from kroger_mcp.tools.shopping_list_tools import (
    _consolidate_items,
    _generate_list_item_id,
    _load_shopping_list,
    _save_shopping_list,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers — pantry context + recipe-preview construction.
# Kept module-private; route handlers are humble shells over these.
# ---------------------------------------------------------------------------


def _pantry_levels(user_id: str) -> dict[str, int]:
    """Best-effort `{product_id: level_percent}` map. Empty on failure."""
    try:
        from kroger_mcp.analytics.pantry import get_pantry_status

        return {
            row["product_id"]: row.get("level_percent", 0)
            for row in get_pantry_status(apply_depletion=True, user_id=user_id)
        }
    except Exception as exc:
        logger.debug("pantry context unavailable: %s", exc)
        return {}


def _category_types(product_ids: list[str]) -> dict[str, str]:
    """Best-effort `{product_id: category_type}` for the given products.

    One batched query feeds the authoritative spice signal so a product whose
    Kroger aisle says "Seasonings & Spices" is gated even when its name alone
    wouldn't match. Empty on failure — callers fall back to name matching.
    """
    ids = [p for p in product_ids if p]
    if not ids:
        return {}
    try:
        from kroger_mcp.analytics.database import get_db_connection

        placeholders = ",".join("?" * len(ids))
        conn = get_db_connection()
        try:
            rows = conn.execute(
                f"SELECT product_id, category_type FROM products "
                f"WHERE product_id IN ({placeholders})",
                ids,
            ).fetchall()
        finally:
            conn.close()
        return {r["product_id"]: r["category_type"] for r in rows if r["category_type"]}
    except Exception as exc:
        logger.debug("category context unavailable: %s", exc)
        return {}


def _round_scaled_qty(raw: float) -> float:
    """Match the in-browser ingredient scaler so previewed qty == committed qty."""
    if raw >= 3:
        return float(round(raw))
    if raw >= 1:
        return round(raw * 2) / 2
    if raw > 0:
        return max(0.25, round(raw * 4) / 4)
    return 1.0


def _build_recipe_preview(recipe: dict, servings: int, pantry: dict[str, int]) -> dict:
    """
    Compute what `add_recipe` would write, without writing.

    Returns `{items_to_add, items_to_skip, manual_purchase, scale_factor}`
    where each item carries the rounded `quantity` the UI will preselect.
    """
    recipe_base = recipe.get("servings", 4) or 4
    scale_factor = servings / recipe_base

    items_to_add: list[dict] = []
    items_to_skip: list[dict] = []
    manual_purchase: list[dict] = []

    for ing in recipe.get("ingredients", []):
        name = ing.get("name", "Unknown")
        unit = ing.get("unit", "")
        product_id = ing.get("product_id")

        try:
            qty_num = float(ing.get("quantity") or 1)
        except (ValueError, TypeError):
            qty_num = 1.0
        scaled_qty = _round_scaled_qty(qty_num * scale_factor)

        # No product_id means the user buys it elsewhere.
        if is_manual_item(ing):
            manual_purchase.append(
                {
                    "name": name,
                    "unit": unit,
                    "quantity": scaled_qty,
                    "original_quantity": ing.get("quantity"),
                    "source": item_source(ing),
                    "notes": ing.get("override_reason"),
                }
            )
            continue

        pantry_level = pantry.get(product_id) if product_id else None
        if product_id and pantry_level is not None and pantry_level >= 30:
            items_to_skip.append(
                {
                    "name": name,
                    "product_id": product_id,
                    "quantity": scaled_qty,
                    "unit": unit,
                    "pantry_level": pantry_level,
                    "reason": f"Pantry at {pantry_level}%",
                }
            )
            continue

        items_to_add.append(
            {
                "name": name,
                "product_id": product_id,
                "quantity": scaled_qty,
                "unit": unit,
                "original_quantity": ing.get("quantity"),
                "pantry_level": pantry_level,
            }
        )

    return {
        "items_to_add": items_to_add,
        "items_to_skip": items_to_skip,
        "manual_purchase": manual_purchase,
        "manual_purchase_by_source": group_by_source(manual_purchase),
        "scale_factor": scale_factor,
    }


# ---------------------------------------------------------------------------
# GET /api/shopping-list
# ---------------------------------------------------------------------------


@router.get("/api/shopping-list")
async def get_shopping_list(request: Request):
    """Return the authenticated user's shopping list."""
    try:
        data = _load_shopping_list(user_id=current_user_id(request))
        return JSONResponse(content=data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load shopping list: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# POST /api/shopping-list/add-recipe
# ---------------------------------------------------------------------------


class SelectedIngredient(BaseModel):
    """User-curated ingredient row from the recipe preview modal."""

    name: str
    product_id: str | None = None
    quantity: float
    # Recipe ingredients with no unit serialise as null; accept both for
    # robustness — anything falsy becomes "" downstream.
    unit: str | None = ""
    # Kept for wire back-compat with older clients. Manual status is derived
    # from a missing product_id, so nothing reads this to make a decision.
    override: bool = False
    # Vendor for an unlinked item, e.g. "Walmart". Free text.
    source: str | None = None


class AddRecipeBody(BaseModel):
    recipe_id: str
    servings_override: int | None = None
    # `confirm` defaults True so MCP/CLI callers keep their current
    # one-shot behaviour. The web UI sends confirm=False first to fetch a
    # preview, then confirm=True with `selections` to commit user edits.
    confirm: bool = True
    selections: list[SelectedIngredient] | None = None


def _resolve_recipe_and_servings(
    body: AddRecipeBody, user_id: str
) -> tuple[dict, int] | JSONResponse:
    from kroger_mcp.tools.recipe_tools import _find_recipe
    from kroger_mcp.tools.shared import get_default_servings

    recipe = _find_recipe(body.recipe_id)
    if not recipe:
        return JSONResponse(
            status_code=404,
            content={"error": f"Recipe '{body.recipe_id}' not found"},
        )
    servings = body.servings_override or get_default_servings(user_id=user_id)
    return recipe, servings


def _commit_recipe_items(
    selections: list[dict],
    recipe: dict,
    recipe_id: str,
    servings: int,
    user_id: str,
) -> int:
    """Write user-selected ingredient rows to the shopping list."""
    listing = _load_shopping_list(user_id=user_id)
    now_iso = datetime.now().isoformat()
    for sel in selections:
        # An unlinked row is a manual item whatever the legacy `override` flag
        # says -- derive it so the two can never disagree.
        is_manual = is_manual_item(sel)
        listing["items"].append(
            {
                "id": _generate_list_item_id(),
                "product_id": None if is_manual else sel["product_id"],
                "ingredient_name": sel["name"],
                "name": sel["name"],
                "quantity": sel["quantity"],
                "unit": sel.get("unit") or "",
                "sources": [
                    {
                        "recipe_id": recipe_id,
                        "recipe_name": recipe.get("name"),
                        "servings_used": servings,
                        "original_quantity": sel.get("original_quantity"),
                        "scaled_quantity": sel["quantity"],
                    }
                ],
                "added_at": now_iso,
                "notes": manual_note(sel) if is_manual else None,
                "manual_purchase": is_manual,
                "source": stored_source(sel.get("source")) if is_manual else None,
                "recipe_name": recipe.get("name"),
            }
        )
    listing["items"] = _consolidate_items(listing["items"])
    _save_shopping_list(listing, user_id=user_id)
    return len(listing["items"])


@router.post("/api/shopping-list/add-recipe")
async def add_recipe_to_list(body: AddRecipeBody, request: Request):
    """
    Three modes:
      - confirm=False                  → return preview (no writes).
      - confirm=True, selections=None  → legacy: auto-add all non-pantry items.
      - confirm=True, selections=[...] → write exactly those rows.

    The pantry-attention session gate is intentionally bypassed here; the
    web UI surfaces pantry levels directly in the preview instead.
    """
    try:
        user_id = current_user_id(request)
        resolved = _resolve_recipe_and_servings(body, user_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        recipe, servings = resolved

        preview = _build_recipe_preview(recipe, servings, _pantry_levels(user_id))

        if not body.confirm:
            return JSONResponse(
                content={
                    "success": True,
                    "confirmation_required": True,
                    "recipe_id": body.recipe_id,
                    "recipe_name": recipe.get("name"),
                    "servings": servings,
                    "items_to_add": preview["items_to_add"],
                    "items_to_skip": preview["items_to_skip"],
                    "manual_purchase": preview["manual_purchase"],
                    "manual_purchase_by_source": preview["manual_purchase_by_source"],
                    "summary": {
                        "to_add": len(preview["items_to_add"]),
                        "to_skip": len(preview["items_to_skip"]),
                        "manual": len(preview["manual_purchase"]),
                    },
                }
            )

        if body.selections is None:
            chosen = [
                {**row, "override": False, "original_quantity": row.get("original_quantity")}
                for row in preview["items_to_add"]
            ] + [
                {**row, "override": True, "product_id": None, "original_quantity": None}
                for row in preview["manual_purchase"]  # product_id=None => manual
            ]
            items_skipped = len(preview["items_to_skip"])
        else:
            chosen = [
                {
                    "name": s.name,
                    # A legacy client sends override=True to mean "don't order
                    # this from Kroger". Honour it by dropping the link, so
                    # is_manual_item derives the same answer downstream instead
                    # of the flag and the product_id contradicting each other.
                    "product_id": None if s.override else s.product_id,
                    "quantity": max(0.25, float(s.quantity)),
                    "unit": s.unit,
                    "override": s.override,
                    "source": s.source,
                    "original_quantity": None,
                }
                for s in body.selections
            ]
            items_skipped = 0

        total = _commit_recipe_items(chosen, recipe, body.recipe_id, servings, user_id)
        return JSONResponse(
            content={
                "success": True,
                "recipe_name": recipe.get("name"),
                "items_added": len(chosen),
                "items_skipped": items_skipped,
                "total_items": total,
                "message": (
                    f"Added {len(chosen)} ingredients from '{recipe.get('name')}' "
                    f"(scaled to {servings} servings)"
                ),
            }
        )

    except Exception as exc:
        logger.exception("add_recipe_to_list failed")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add recipe: {exc}"},
        )


# ---------------------------------------------------------------------------
# POST /api/shopping-list/items  (add a single item)
# Used by the action_menu 'Add to List' action on product + favorite cards.
# Consolidation merges duplicates by product_id so repeated clicks bump qty
# instead of creating parallel rows.
# ---------------------------------------------------------------------------


class AddItemBody(BaseModel):
    product_id: str | None = None
    name: str
    quantity: float = 1.0
    unit: str = ""
    # Kept for wire back-compat. An item with no product_id is manual whether
    # or not this is set; is_manual_item is the authority.
    manual_purchase: bool = False
    # Vendor for a manual item, e.g. "Walmart". Free text; groups the item into
    # its own section of the shopping list.
    source: str | None = None


@router.post("/api/shopping-list/items")
async def add_shopping_list_item(body: AddItemBody, request: Request):
    """Append a manual item to the authenticated user's shopping list."""
    try:
        user_id = current_user_id(request)
        listing = _load_shopping_list(user_id=user_id)
        # manual_purchase=True with no product_id is the same thing; derive it
        # so the flag and the link can never disagree.
        product_id = None if body.manual_purchase else body.product_id
        is_manual = is_manual_item({"product_id": product_id})
        new_item: dict[str, Any] = {
            "id": _generate_list_item_id(),
            "product_id": product_id,
            "name": body.name,
            "ingredient_name": body.name,
            "quantity": body.quantity,
            "unit": body.unit,
            "added_at": datetime.now().isoformat(),
            "notes": manual_note({"source": body.source}) if is_manual else None,
            "manual_purchase": is_manual,
            "source": stored_source(body.source) if is_manual else None,
            "sources": [],
        }
        listing["items"].append(new_item)
        listing["items"] = _consolidate_items(listing["items"])
        _save_shopping_list(listing, user_id=user_id)
        return JSONResponse(
            content={
                "success": True,
                "item_id": new_item["id"],
                "total_items": len(listing["items"]),
            }
        )
    except Exception as exc:
        logger.exception("add_shopping_list_item failed")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add item: {exc}"},
        )


# ---------------------------------------------------------------------------
# DELETE /api/shopping-list  (clear-all)
# Must be registered before /{item_id} so FastAPI doesn't treat the empty
# path as an item_id match.
# ---------------------------------------------------------------------------


@router.delete("/api/shopping-list")
async def clear_shopping_list(request: Request):
    """Clear all items from the authenticated user's shopping list."""
    try:
        user_id = current_user_id(request)
        data = _load_shopping_list(user_id=user_id)
        count = len(data["items"])
        data["items"] = []
        _save_shopping_list(data, user_id=user_id)
        return JSONResponse(content={"success": True, "cleared": count})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to clear shopping list: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# DELETE /api/shopping-list/{item_id}
# ---------------------------------------------------------------------------


@router.delete("/api/shopping-list/{item_id}")
async def remove_shopping_list_item(item_id: str, request: Request):
    """Remove a single item from the authenticated user's shopping list."""
    try:
        user_id = current_user_id(request)
        data = _load_shopping_list(user_id=user_id)
        original_count = len(data["items"])
        data["items"] = [
            item
            for item in data["items"]
            if item.get("id") != item_id and item.get("product_id") != item_id
        ]
        removed = original_count - len(data["items"])
        if removed == 0:
            return JSONResponse(
                status_code=404,
                content={"error": f"Item '{item_id}' not found"},
            )
        _save_shopping_list(data, user_id=user_id)
        return JSONResponse(
            content={
                "success": True,
                "removed": item_id,
                "remaining": len(data["items"]),
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove item: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# PATCH /api/shopping-list/{item_id}
# ---------------------------------------------------------------------------


class UpdateItemBody(BaseModel):
    quantity: float | None = None
    notes: str | None = None


@router.patch("/api/shopping-list/{item_id}")
async def update_shopping_list_item(item_id: str, body: UpdateItemBody, request: Request):
    """Update quantity or notes for one of the authenticated user's items."""
    try:
        user_id = current_user_id(request)
        data = _load_shopping_list(user_id=user_id)
        found = False
        for item in data["items"]:
            if item.get("id") == item_id:
                found = True
                if body.quantity is not None:
                    item["quantity"] = body.quantity
                if body.notes is not None:
                    item["notes"] = body.notes
                item["last_updated"] = datetime.now().isoformat()
                break

        if not found:
            return JSONResponse(
                status_code=404,
                content={"error": f"Item '{item_id}' not found"},
            )

        _save_shopping_list(data, user_id=user_id)
        return JSONResponse(content={"success": True, "item_id": item_id})

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update item: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# POST /api/shopping-list/add-to-cart
# ---------------------------------------------------------------------------


class SelectedCartItem(BaseModel):
    """User-confirmed cart row: which product, how many."""

    product_id: str
    quantity: int


class AddToCartBody(BaseModel):
    confirm: bool = False
    confirm_unsafe: bool = False
    modality: str = "PICKUP"
    # Product IDs from the modal's Spices section the user ticked. None on
    # the initial preview round-trip; populated by the client on confirm.
    included_spice_ids: list[str] | None = None
    # Per-row {product_id, quantity} chosen via the modal's checkbox +
    # stepper UI. When confirm=True and selections is provided, only these
    # product_ids are sent and only they are cleared from the local list;
    # unknown product_ids are silently dropped (treated as stale).
    selections: list[SelectedCartItem] | None = None


def _apply_cart_selections(
    items_to_add: list[dict],
    selections: list[SelectedCartItem],
) -> list[dict]:
    """Intersect server-computed cart items with the user's curated picks."""
    overrides = {s.product_id: max(1, int(s.quantity)) for s in selections}
    return [
        {**row, "quantity": overrides[row["product_id"]]}
        for row in items_to_add
        if row["product_id"] in overrides
    ]


@router.post("/api/shopping-list/add-to-cart")
async def shopping_list_to_cart(body: AddToCartBody, request: Request):
    """
    confirm=False → return preview of items to be added.
    confirm=True  → add items to Kroger cart, update local tracking, clear list.

    Items classified as spices (see analytics.ingredients.is_spice) are split
    into their own preview bucket so the modal can present them as opt-in
    checkboxes. Only spices whose product_id appears in
    ``body.included_spice_ids`` are promoted into the actual cart submission.
    """
    try:
        from kroger_mcp.analytics.ingredients import classify_spice
        from kroger_mcp.tools.shared import get_include_spices_by_default

        user_id = current_user_id(request)
        data = _load_shopping_list(user_id=user_id)
        items = data.get("items", [])

        if not items:
            return JSONResponse(
                content={
                    "success": True,
                    "message": "Shopping list is empty",
                    "items": [],
                }
            )

        pantry_context = _pantry_levels(user_id)
        category_by_pid = _category_types([it.get("product_id") for it in items])

        spice_default_included = get_include_spices_by_default(user_id=user_id)

        items_to_add = []
        items_to_skip = []
        items_manual = []
        items_spices = []

        for item in items:
            product_id = item.get("product_id")
            name = item.get("name") or item.get("ingredient_name") or product_id

            # No product_id means the user buys it elsewhere -- a manual item,
            # not a half-finished Kroger one.
            if is_manual_item(item):
                items_manual.append(
                    {
                        "product_id": None,
                        "name": name,
                        "quantity": item.get("quantity", 1),
                        "unit": item.get("unit", ""),
                        "source": item_source(item),
                        "notes": item.get("notes"),
                    }
                )
                continue

            pantry_level = pantry_context.get(product_id)
            if pantry_level is not None and pantry_level >= 30:
                items_to_skip.append(
                    {
                        "product_id": product_id,
                        "name": name,
                        "reason": f"Pantry at {pantry_level}%",
                    }
                )
                continue

            normalized_entry = {
                "product_id": product_id,
                "name": name,
                "quantity": max(1, round(item.get("quantity", 1))),
                "recipe_name": item.get("recipe_name")
                or (
                    item.get("sources", [{}])[0].get("recipe_name") if item.get("sources") else None
                ),
            }

            if classify_spice(name, category_type=category_by_pid.get(product_id)):
                items_spices.append(
                    {**normalized_entry, "default_included": spice_default_included}
                )
            else:
                items_to_add.append(normalized_entry)

        # Preview mode
        if not body.confirm:
            return JSONResponse(
                content={
                    "success": True,
                    "confirmation_required": True,
                    "items": items_to_add,
                    "items_to_skip": items_to_skip,
                    "manual_purchase": items_manual,
                    "manual_purchase_by_source": group_by_source(items_manual),
                    "spices": items_spices,
                    "summary": {
                        "to_add": len(items_to_add),
                        "to_skip": len(items_to_skip),
                        "manual": len(items_manual),
                        "spices": len(items_spices),
                        "spice_default_included": spice_default_included,
                    },
                }
            )

        # Promote any spices the user explicitly ticked into the cart batch
        # BEFORE applying per-row selections, so a single `selections` list
        # can curate both regular items and opted-in spices.
        spice_id_set = set(body.included_spice_ids or [])
        if spice_id_set:
            for spice in items_spices:
                if spice["product_id"] in spice_id_set:
                    items_to_add.append(
                        {
                            "product_id": spice["product_id"],
                            "name": spice["name"],
                            "quantity": spice["quantity"],
                            "recipe_name": spice.get("recipe_name"),
                        }
                    )

        if body.selections is not None:
            items_to_add = _apply_cart_selections(items_to_add, body.selections)

        if not items_to_add:
            return JSONResponse(
                content={
                    "success": True,
                    "message": "No purchasable items (all stocked or missing product IDs)",
                    "items_added": 0,
                }
            )

        from kroger_mcp.tools._cart_safety import check_cart_items_safety
        from kroger_mcp.tools.cart_tools import _add_item_to_local_cart
        from kroger_mcp.tools.shared import get_authenticated_client

        safety_response = check_cart_items_safety(
            [
                {"product_id": it["product_id"], "description": it.get("name", "")}
                for it in items_to_add
            ],
            user_id=user_id,
            confirm_unsafe=body.confirm_unsafe,
        )
        if safety_response is not None:
            return safety_response

        client = await asyncio.to_thread(get_authenticated_client, user_id)
        api_items = [
            {"upc": it["product_id"], "quantity": it["quantity"], "modality": body.modality}
            for it in items_to_add
        ]

        # Add to Kroger cart with per-item fallback on 400
        failed_items = []
        added_items = list(items_to_add)
        try:
            await asyncio.to_thread(client.cart.add_to_cart, api_items)
        except Exception as batch_err:
            batch_err_str = str(batch_err)
            is_400 = "400" in batch_err_str or "Bad Request" in batch_err_str
            is_401 = "401" in batch_err_str or "Unauthorized" in batch_err_str
            if is_401:
                raise
            if is_400 and len(api_items) > 1:
                # Retry each item individually
                added_items = []
                for api_item, orig_item in zip(api_items, items_to_add, strict=False):
                    try:
                        await asyncio.to_thread(client.cart.add_to_cart, [api_item])
                        added_items.append(orig_item)
                    except Exception:
                        failed_items.append(orig_item)
            else:
                raise

        added_ids = {it["product_id"] for it in added_items}

        # Stamp snacks' last_ordered_at so the next check-up sees them as fresh.
        # No-op for non-snack products (scoped to the user's snack-type lists).
        try:
            from kroger_mcp.analytics.favorites import mark_snacks_ordered

            mark_snacks_ordered(list(added_ids), user_id=user_id)
        except Exception:
            logger.warning("mark_snacks_ordered failed; staleness signal not updated")

        # The real Kroger order above already succeeded — a local-tracking
        # failure must never flip this response to success=False, or a
        # caller retrying on "failure" would place a duplicate real order —
        # but it must be logged and surfaced, not silently swallowed.
        local_tracking_errors = []
        for it in added_items:
            try:
                _add_item_to_local_cart(
                    product_id=it["product_id"],
                    quantity=it["quantity"],
                    modality=body.modality,
                    product_details={"description": it.get("name")},
                    user_id=user_id,
                )
            except Exception as tracking_err:
                logger.error(
                    "Local cart tracking failed for %s after a successful Kroger order: %s",
                    it["product_id"],
                    tracking_err,
                )
                local_tracking_errors.append(it["product_id"])

        # Clear successfully-added items from the list; keep manual and failed items
        data["items"] = [
            item
            for item in data["items"]
            if item.get("manual_purchase")
            or not item.get("product_id")
            or item.get("product_id") not in added_ids
        ]
        _save_shopping_list(data, user_id=user_id)

        result = {
            "success": True,
            "items_added": len(added_items),
            "items_skipped": len(items_to_skip),
            "manual_purchase": items_manual,
            "manual_purchase_by_source": group_by_source(items_manual),
            "message": f"Added {len(added_items)} items to your Kroger cart.",
        }
        if failed_items:
            result["items_failed"] = len(failed_items)
            result["failed_items"] = [f["name"] for f in failed_items]
            result["warning"] = (
                f"{len(failed_items)} item(s) rejected by Kroger API "
                "(invalid product ID or not available at this location)"
            )
        if local_tracking_errors:
            result["local_tracking_warning"] = (
                "Kroger order succeeded, but local cart tracking failed "
                f"for {len(local_tracking_errors)} item(s): {local_tracking_errors}"
            )
        return JSONResponse(content=result)

    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err or "Authentication" in err:
            return JSONResponse(
                status_code=401,
                content={"error": "Not authenticated. Please log in via Claude first."},
            )
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to send to cart: {err}"},
        )
