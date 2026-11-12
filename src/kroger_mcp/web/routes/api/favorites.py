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
    """A favorites item to add.

    `product_id` is omitted for a manual item — something Kroger doesn't sell.
    Such an item is stored with a synthetic id and never sent to the Kroger cart.
    """

    product_id: str | None = None
    description: str
    brand: str | None = None
    quantity: int = 1
    notes: str | None = None
    manual: bool = False
    override_reason: str | None = None


class UpdateItemBody(BaseModel):
    default_quantity: int | None = None
    typical_gap_days: int | None = None


@router.get("/api/favorites/lists")
async def get_favorites_lists(request: Request):
    """Return all favorites lists owned by the authenticated user."""
    try:
        from kroger_mcp.analytics.favorites import get_lists

        lists = get_lists(user_id=current_user_id(request))
        return JSONResponse(
            content=[
                {
                    "id": lst["id"],
                    "name": lst["name"],
                    "item_count": lst["item_count"],
                    "list_type": lst["list_type"],
                }
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
            rw_result = update_list_schedule(list_id=list_id, reorder_weeks=weeks, user_id=owner)
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
    """Add a product — or a manual, non-Kroger item — to a favorite list."""
    try:
        from kroger_mcp.analytics.favorites import add_to_list

        if not body.product_id and not body.manual:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "product_id is required unless the item is marked manual",
                },
            )

        result = add_to_list(
            list_id=list_id,
            product_id=body.product_id,
            description=body.description,
            brand=body.brand,
            default_quantity=body.quantity,
            notes=body.notes,
            manual=body.manual,
            override_reason=body.override_reason,
            user_id=current_user_id(request),
        )
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.patch("/api/favorites/lists/{list_id}/items/{product_id}")
async def update_item(list_id: str, product_id: str, body: UpdateItemBody, request: Request):
    """Update a favorite item's default order quantity and/or typical gap days.

    Only the fields present in the request are updated. Integer counts clamp to a
    minimum of 1 and the saved values are echoed back so the client can reconcile
    if it sent something out of range.
    """
    try:
        from kroger_mcp.analytics.favorites import update_list_item

        updates: dict[str, int] = {}
        if body.default_quantity is not None:
            updates["default_quantity"] = max(1, body.default_quantity)
        if body.typical_gap_days is not None:
            updates["typical_gap_days"] = max(1, body.typical_gap_days)

        if not updates:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "No fields to update"},
            )

        result = update_list_item(
            list_id=list_id,
            product_id=product_id,
            user_id=current_user_id(request),
            **updates,
        )
        if not result.get("success"):
            return JSONResponse(status_code=404, content=result)
        return {"success": True, **updates}
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
    from kroger_mcp.tools.shopping_list_ops import add_favorites_list_to_list

    try:
        result = add_favorites_list_to_list(list_id, current_user_id(request))
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    if not result.get("success"):
        return JSONResponse(status_code=404, content=result)
    return result


class AddSnacksBody(BaseModel):
    """Snacks the user ticked in the pre-cart check-up, by product_id."""

    product_ids: list[str]


@router.get("/api/favorites/snacks/check")
async def check_snacks_route(request: Request):
    """Return the pre-cart snack replenishment checklist for the user."""
    from kroger_mcp.analytics.favorites import check_snacks as _check_snacks

    try:
        return _check_snacks(user_id=current_user_id(request))
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/favorites/snacks/add-to-list")
async def add_snacks_to_shopping_list(body: AddSnacksBody, request: Request):
    """Append the chosen snacks (by product_id) to the shopping list.

    Only product_ids that are genuinely on a snacks list are honored — the
    check-up snapshot is the allow-list, so a stale/forged id is ignored.
    """
    from kroger_mcp.tools.shopping_list_ops import add_snacks_to_list

    return add_snacks_to_list(body.product_ids, current_user_id(request))
