"""Shopping-list writes shared by the web routes and the `shopping_list` /
`favorites` MCP tools.

Each function used to live inside a FastAPI handler; it moved here so the web
app and an agent run the exact same logic and can't drift apart. All of them
read and write only `user_id`'s list.
"""

import logging
from datetime import datetime
from typing import Any

from ..analytics.manual_sources import is_manual_item, manual_note, stored_source
from .shopping_list_tools import (
    _consolidate_items,
    _generate_list_item_id,
    _load_shopping_list,
    _save_shopping_list,
)

logger = logging.getLogger(__name__)

# A favorite/pantry item at or above this level is skipped when a whole
# favorites list is sent to the shopping list.
WELL_STOCKED_PERCENT = 30


def add_list_item(
    user_id: str,
    name: str,
    product_id: str | None = None,
    quantity: float = 1.0,
    unit: str = "",
    source: str | None = None,
) -> dict[str, Any]:
    """Append one item; an item with no product_id is a manual purchase."""
    listing = _load_shopping_list(user_id=user_id)
    is_manual = is_manual_item({"product_id": product_id})
    new_item: dict[str, Any] = {
        "id": _generate_list_item_id(),
        "product_id": product_id,
        "name": name,
        "ingredient_name": name,
        "quantity": quantity,
        "unit": unit,
        "added_at": datetime.now().isoformat(),
        "notes": manual_note({"source": source}) if is_manual else None,
        "manual_purchase": is_manual,
        "source": stored_source(source) if is_manual else None,
        "sources": [],
    }
    listing["items"].append(new_item)
    listing["items"] = _consolidate_items(listing["items"])
    _save_shopping_list(listing, user_id=user_id)
    return {"item_id": new_item["id"], "total_items": len(listing["items"])}


def clear_list(user_id: str) -> int:
    """Empty the list; return how many items were removed."""
    data = _load_shopping_list(user_id=user_id)
    count = len(data["items"])
    data["items"] = []
    _save_shopping_list(data, user_id=user_id)
    return count


def mark_list_items_empty(user_id: str) -> dict[str, int]:
    """Set every listed product's pantry level to 0%. The list is untouched.

    Goes through `update_pantry_level` so each one records a depletion event,
    exactly as a manual 0% set would. Items not tracked in the pantry, and
    manual items (no product_id), are skipped rather than added.
    """
    from ..analytics.pantry import update_pantry_level

    items = _load_shopping_list(user_id=user_id)["items"]
    product_ids = list(dict.fromkeys(i["product_id"] for i in items if i.get("product_id")))
    emptied = sum(
        1 for pid in product_ids if update_pantry_level(pid, 0, user_id=user_id).get("success")
    )
    return {"emptied": emptied, "skipped": len(items) - emptied}


def _favorite_row(item: dict[str, Any], quantity: Any, sources: list[dict[str, Any]], now: str) -> dict[str, Any]:
    """Shopping-list row for a favorite; a manual favorite becomes an unlinked
    manual_purchase row so the cart-send path lists it instead of buying it."""
    manual = bool(item.get("is_manual"))
    return {
        "id": _generate_list_item_id(),
        "product_id": None if manual else item["product_id"],
        "name": item.get("description", ""),
        "quantity": quantity or 1,
        "unit": "",
        "sources": sources,
        "added_at": now,
        "notes": manual_note(item) if manual else None,
        "manual_purchase": manual,
        "source": stored_source(item.get("source")) if manual else None,
    }


def add_favorites_list_to_list(list_id: str, user_id: str) -> dict[str, Any]:
    """Add a favorites list's items to the shopping list, skipping well-stocked ones.

    Returns the favorites lookup's own failure dict (success False) when the
    list doesn't exist or isn't `user_id`'s.
    """
    from ..analytics.favorites import get_list_items, mark_list_ordered
    from ..analytics.pantry import get_pantry_status

    result = get_list_items(list_id=list_id, include_pantry_status=True, user_id=user_id)
    if not result.get("success"):
        return result

    list_name = result.get("list", {}).get("name", list_id)
    try:
        pantry_levels = {
            p["product_id"]: p.get("level_percent", 0)
            for p in get_pantry_status(apply_depletion=True, user_id=user_id)
        }
    except Exception as exc:  # no pantry data: skip nothing
        logger.warning("pantry levels unavailable for favorites add: %s", exc)
        pantry_levels = {}

    data = _load_shopping_list(user_id=user_id)
    now = datetime.now().isoformat()
    added = skipped = manual_count = 0
    for item in result.get("items", []):
        if not item.get("product_id"):
            continue
        manual = bool(item.get("is_manual"))
        if not manual and pantry_levels.get(item["product_id"], 0) >= WELL_STOCKED_PERCENT:
            skipped += 1
            continue
        sources = [{"favorites_list_id": list_id, "favorites_list_name": list_name}]
        row = _favorite_row(item, item.get("default_quantity"), sources, now)
        row["recipe_name"] = None
        data["items"].append(row)
        added += 1
        manual_count += 1 if manual else 0

    data["items"] = _consolidate_items(data["items"])
    _save_shopping_list(data, user_id=user_id)
    if added:
        mark_list_ordered(list_id, user_id=user_id)

    return {
        "success": True,
        "list_name": list_name,
        "items_added": added,
        "items_skipped": skipped,
        "items_manual": manual_count,
        "total_items": len(data["items"]),
    }


def add_snacks_to_list(product_ids: list[str], user_id: str) -> dict[str, Any]:
    """Append chosen snacks to the shopping list.

    Only ids that are genuinely on a snacks list are honored: the snack
    check-up snapshot is the allow-list, so a stale or forged id is ignored.
    """
    from ..analytics import favorites

    chosen = set(product_ids)
    if not chosen:
        return {"success": True, "items_added": 0}

    by_id = {c["product_id"]: c for c in favorites.check_snacks(user_id=user_id).get("candidates", [])}
    data = _load_shopping_list(user_id=user_id)
    now = datetime.now().isoformat()
    added = 0
    for product_id in chosen:
        snack = by_id.get(product_id)
        if not snack:
            continue
        sources = [{"snacks_list_id": snack["list_id"]}]
        data["items"].append(_favorite_row(snack, snack.get("default_quantity"), sources, now))
        added += 1

    data["items"] = _consolidate_items(data["items"])
    _save_shopping_list(data, user_id=user_id)
    return {"success": True, "items_added": added, "total_items": len(data["items"])}
