"""API routes for favorites list write operations."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()


class CreateListBody(BaseModel):
    name: str
    description: str | None = None
    list_type: str = "custom"
    reorder_weeks: int | None = None


class RenameListBody(BaseModel):
    name: str | None = None
    description: str | None = None
    reorder_weeks: int | None = None


class AddItemBody(BaseModel):
    product_id: str
    description: str
    brand: str | None = None
    quantity: int = 1
    notes: str | None = None


@router.get("/api/favorites/lists")
async def get_favorites_lists(request: Request):
    """Return all favorites lists owned by the authenticated user."""
    try:
        from kroger_mcp.analytics.favorites import get_lists

        lists = get_lists(user_id=current_user_id(request))
        return JSONResponse(
            content=[
                {"id": lst["id"], "name": lst["name"], "item_count": lst["item_count"]}
                for lst in lists
                if not lst.get("is_default")
            ]
        )
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/favorites/lists")
async def create_list(body: CreateListBody, request: Request):
    """Create a favorites list owned by the authenticated user."""
    try:
        from kroger_mcp.analytics.favorites import create_list as _create_list

        result = _create_list(
            name=body.name,
            description=body.description,
            list_type=body.list_type,
            reorder_weeks=body.reorder_weeks,
            user_id=current_user_id(request),
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content=result,
            )
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/favorites/lists/{list_id}")
async def delete_list(list_id: str, request: Request):
    """Delete a favorites list owned by the authenticated user."""
    try:
        from kroger_mcp.analytics.favorites import delete_list as _delete_list

        result = _delete_list(list_id=list_id, user_id=current_user_id(request))
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.put("/api/favorites/lists/{list_id}")
async def rename_list(list_id: str, body: RenameListBody, request: Request):
    """Rename a list, update its description, or update reorder schedule."""
    try:
        owner = current_user_id(request)
        errors = []

        if body.name is not None or body.description is not None:
            from kroger_mcp.analytics.favorites import rename_list as _rename_list

            result = _rename_list(
                list_id=list_id,
                new_name=body.name,
                new_description=body.description,
                user_id=owner,
            )
            if not result.get("success"):
                errors.append(result.get("error", "Rename failed"))

        if body.reorder_weeks is not None:
            from kroger_mcp.analytics.favorites import update_list_schedule

            # 0 means "disable schedule", positive int means set schedule
            weeks = None if body.reorder_weeks == 0 else body.reorder_weeks
            rw_result = update_list_schedule(
                list_id=list_id, reorder_weeks=weeks, user_id=owner
            )
            if not rw_result.get("success"):
                errors.append(rw_result.get("error", "Schedule update failed"))

        if errors:
            return JSONResponse(status_code=400, content={"error": "; ".join(errors)})
        return {"success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/api/favorites/lists/{list_id}/items")
async def get_list_items(list_id: str, request: Request):
    """Get items in a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import get_list_items as _get_list_items

        result = _get_list_items(list_id=list_id, user_id=current_user_id(request))
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/favorites/lists/{list_id}/items")
async def add_item(list_id: str, body: AddItemBody, request: Request):
    """Add a product to a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import add_to_list

        result = add_to_list(
            list_id=list_id,
            product_id=body.product_id,
            description=body.description,
            brand=body.brand,
            default_quantity=body.quantity,
            notes=body.notes,
            user_id=current_user_id(request),
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/favorites/lists/{list_id}/items/{product_id}")
async def remove_item(list_id: str, product_id: str, request: Request):
    """Remove a product from a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import remove_from_list

        result = remove_from_list(
            list_id=list_id, product_id=product_id, user_id=current_user_id(request)
        )
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/favorites/lists/{list_id}/add-to-shopping-list")
async def add_list_to_shopping_list(list_id: str, request: Request):
    """Add all items from a favorites list into the shopping list, skipping well-stocked items."""
    from datetime import datetime

    from kroger_mcp.analytics.favorites import get_list_items as _get_list_items
    from kroger_mcp.tools.shopping_list_tools import (
        _consolidate_items,
        _generate_list_item_id,
        _load_shopping_list,
        _save_shopping_list,
    )

    # Load list items
    try:
        result = _get_list_items(
            list_id=list_id, include_pantry_status=True, user_id=current_user_id(request)
        )
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    if not result.get("success"):
        return JSONResponse(status_code=404, content=result)

    list_name = result.get("list", {}).get("name", list_id)
    items = result.get("items", [])

    # Load pantry levels for skip logic
    pantry_levels: dict = {}
    try:
        from kroger_mcp.analytics.pantry import get_pantry_status

        pantry_items = get_pantry_status(apply_depletion=True)
        pantry_levels = {p["product_id"]: p.get("level_percent", 0) for p in pantry_items}
    except Exception:
        pass  # No pantry data — skip nothing

    # Build new shopping list entries
    data = _load_shopping_list()
    items_added = 0
    items_skipped = 0
    now = datetime.now().isoformat()

    for item in items:
        product_id = item.get("product_id")
        if not product_id:
            continue
        level = pantry_levels.get(product_id, 0)
        if level >= 30:
            items_skipped += 1
            continue
        data["items"].append(
            {
                "id": _generate_list_item_id(),
                "product_id": product_id,
                "name": item.get("description", ""),
                "quantity": item.get("default_quantity") or 1,
                "unit": "",
                "sources": [{"favorites_list_id": list_id, "favorites_list_name": list_name}],
                "added_at": now,
                "recipe_name": None,
            }
        )
        items_added += 1

    data["items"] = _consolidate_items(data["items"])
    _save_shopping_list(data)

    return {
        "success": True,
        "list_name": list_name,
        "items_added": items_added,
        "items_skipped": items_skipped,
        "total_items": len(data["items"]),
    }
