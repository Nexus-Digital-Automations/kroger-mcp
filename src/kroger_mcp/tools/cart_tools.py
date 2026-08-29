"""
Cart tracking and management functionality
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ..analytics.deals import calculate_cart_savings, record_price_observation
from ._cart_safety import check_cart_items_safety
from .shared import get_authenticated_client, get_preferred_location_id

logger = logging.getLogger(__name__)

_cart_write_locks: dict[str, threading.Lock] = {}
_cart_write_locks_guard = threading.Lock()


def _cart_write_lock(user_id: str | None) -> threading.Lock:
    """Per-user lock serializing cart read-modify-write sequences.

    _save_cart_data replaces a user's whole cart in one DELETE+INSERT pass, so
    two concurrent load->mutate->save sequences for the same user (e.g. two
    near-simultaneous add-to-cart calls) can race and silently lose one of
    them (last save wins on the full snapshot). This only serializes within
    one worker process -- web/app.py can run multiple gunicorn worker
    processes, so a cross-process race remains possible in principle.
    Acceptable given cart writes are low-frequency, human-paced actions.
    """
    owner = _resolve_cart_user_id(user_id)
    with _cart_write_locks_guard:
        lock = _cart_write_locks.get(owner)
        if lock is None:
            lock = threading.Lock()
            _cart_write_locks[owner] = lock
        return lock


def _get_session_id(ctx) -> str:
    """Extract session ID from MCP context."""
    if ctx and hasattr(ctx, "session_id"):
        return str(ctx.session_id)
    return "default"


def _resolve_cart_user_id(user_id: str | None) -> str:
    """Resolve user_id for cart operations.

    None falls back to mcp_user_id() — picks up KROGER_MCP_USER_ID per
    Claude Desktop profile, falls back to the migration-installed owner.
    HTTP-route callers should pass user_id explicitly via current_user_id().
    """
    from kroger_mcp.auth.dependencies import mcp_user_id

    return user_id if user_id is not None else mcp_user_id()


def _load_cart_data(user_id: str) -> dict[str, Any]:
    """Return this user's cart in the legacy `{current_cart, last_updated}` shape."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_cart_user_id(user_id)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT product_id, description, quantity, modality, added_at,
                   regular_price, sale_price
            FROM user_carts WHERE user_id = ?
            ORDER BY added_at
            """,
            (owner,),
        ).fetchall()
        current_cart = [
            {
                "product_id": row["product_id"],
                "description": row["description"],
                "quantity": row["quantity"],
                "modality": row["modality"],
                "added_at": row["added_at"],
                "last_updated": row["added_at"],
                "regular_price": row["regular_price"],
                "price": row["sale_price"],
            }
            for row in rows
        ]
        last_updated = rows[-1]["added_at"] if rows else None
        return {
            "current_cart": current_cart,
            "last_updated": last_updated,
            "preferred_location_id": None,
        }
    finally:
        conn.close()


def _save_cart_data(cart_data: dict[str, Any], user_id: str) -> None:
    """Replace this user's cart with the items in `cart_data["current_cart"]`."""
    from kroger_mcp.analytics.database import get_db_connection

    owner = _resolve_cart_user_id(user_id)
    items = cart_data.get("current_cart", []) or []
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_carts WHERE user_id = ?", (owner,))
        for item in items:
            pid = item.get("product_id")
            if not pid:
                continue
            conn.execute(
                """
                INSERT INTO user_carts
                    (user_id, product_id, description, quantity, modality, added_at,
                     regular_price, sale_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, product_id) DO UPDATE SET
                    description = excluded.description,
                    quantity = excluded.quantity,
                    modality = excluded.modality,
                    added_at = excluded.added_at,
                    regular_price = excluded.regular_price,
                    sale_price = excluded.sale_price
                """,
                (
                    owner,
                    pid,
                    item.get("description"),
                    int(item.get("quantity", 1) or 1),
                    item.get("modality", "PICKUP"),
                    item.get("added_at") or datetime.now().isoformat(),
                    item.get("regular_price"),
                    item.get("price"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _add_item_to_local_cart(
    product_id: str,
    quantity: int,
    modality: str,
    product_details: dict[str, Any] | None = None,
    *, user_id: str,
) -> None:
    """Add an item to the local cart tracking and analytics database.

    Raises ValueError if `product_id` is a manual favorite's synthetic id. Every
    local-cart row is eventually shipped to Kroger as a `upc`, and this function
    is the one chokepoint all nine cart-add paths funnel through — the paths that
    legitimately carry manual items (the favorites `order` action, the
    shopping-list and recipe pushes) filter them out well before here, so
    reaching this line at all means a caller lost track of one.
    """
    from ..analytics.favorites import is_manual_product_id

    if is_manual_product_id(product_id):
        raise ValueError(
            f"{product_id} is a manual item not sold at Kroger and cannot be added "
            "to the cart"
        )

    with _cart_write_lock(user_id):
        cart_data = _load_cart_data(user_id=user_id)
        current_cart = cart_data.get("current_cart", [])

        existing_item = None
        for item in current_cart:
            if item.get("product_id") == product_id and item.get("modality") == modality:
                existing_item = item
                break

        if existing_item:
            existing_item["quantity"] = existing_item.get("quantity", 0) + quantity
            existing_item["last_updated"] = datetime.now().isoformat()
        else:
            new_item = {
                "product_id": product_id,
                "quantity": quantity,
                "modality": modality,
                "added_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat(),
            }
            if product_details:
                new_item.update(product_details)
            current_cart.append(new_item)

        cart_data["current_cart"] = current_cart
        cart_data["last_updated"] = datetime.now().isoformat()
        _save_cart_data(cart_data, user_id=user_id)

    try:
        from ..analytics.purchase_tracker import record_cart_add

        record_cart_add(product_id, quantity, modality, product_details, user_id=user_id)
    except Exception as e:
        logger.warning("Could not record analytics: %s", e)

    if product_details:
        try:
            regular_price = product_details.get("regular_price")
            sale_price = product_details.get("sale_price") or product_details.get("price")
            location_id = get_preferred_location_id(user_id=user_id)
            if (regular_price or sale_price) and location_id:
                record_price_observation(
                    product_id=product_id,
                    regular_price=regular_price,
                    sale_price=sale_price,
                    location_id=location_id,
                    source="cart_add",
                )
        except Exception:
            pass

    try:
        from ..analytics.pantry import add_to_pantry

        description = (product_details or {}).get("description") or None
        add_to_pantry(product_id=product_id, description=description, user_id=user_id)
    except Exception as e:
        logger.warning("Could not add product %s to pantry: %s", product_id, e)


def register_tools(mcp):
    """Register cart-related tools with the FastMCP server"""

    @mcp.tool()
    async def cart(
        action: Literal[
            "view",
            "add",
            "remove",
            "clear",
            "mark_placed",
            "view_history",
            "get_context",
        ] = Field(
            description=(
                "add — supports BATCH via items=[...] (max 50). "
                "To order a favorites list, use favorites(action='order') instead. "
                "Other: view|remove|clear|mark_placed|view_history|get_context"
            )
        ),
        product_id: str | None = Field(
            default=None,
            description="Product ID",
        ),
        quantity: int | None = Field(
            default=1,
            description="Quantity to add 1-99",
        ),
        modality: str | None = Field(
            default="PICKUP",
            description="PICKUP or DELIVERY",
        ),
        items: list[dict[str, Any]] | None = Field(
            default=None,
            description=(
                "PREFERRED for multi-item adds. List of dicts: "
                "[{product_id, quantity, modality, description}] max 50. "
                "Always use this instead of calling add multiple times."
            ),
        ),
        preview_only: bool | None = Field(
            default=True,
            description="True=preview only (default). Set False to actually add to cart.",
        ),
        confirm_unsafe: bool | None = Field(
            default=False,
            description="Override safety warnings",
        ),
        order_notes: str | None = Field(
            default=None,
            description="Order notes",
        ),
        limit: int | None = Field(
            default=10,
            description="Recent orders to show 1-50",
        ),
        product_ids: list[str] | None = Field(
            default=None,
            description="Product IDs to check context for",
        ),
        pantry_threshold: int | None = Field(
            default=30,
            description="Skip items above this pantry %",
        ),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Cart management operations.

        BATCH ADD: Use items=[{product_id, quantity, modality}, ...] to add up to 50
        items in ONE call. Never loop single adds.

        TO ORDER A FAVORITES LIST: Use favorites(action='order', list_id='...') instead
        of manually compiling items — it handles pantry skipping automatically.
        """
        from kroger_mcp.auth.dependencies import mcp_user_id

        user_id = mcp_user_id()

        match action:
            case "view":
                try:
                    cart_data = _load_cart_data(user_id=user_id)
                    current_cart = cart_data.get("current_cart", [])

                    total_quantity = sum(item.get("quantity", 0) for item in current_cart)
                    pickup_items = [i for i in current_cart if i.get("modality") == "PICKUP"]
                    delivery_items = [i for i in current_cart if i.get("modality") == "DELIVERY"]

                    savings_summary = None
                    try:
                        savings_summary = calculate_cart_savings(current_cart)
                    except Exception:
                        pass

                    result = {
                        "success": True,
                        "current_cart": current_cart,
                        "summary": {
                            "total_items": len(current_cart),
                            "total_quantity": total_quantity,
                            "pickup_items": len(pickup_items),
                            "delivery_items": len(delivery_items),
                            "last_updated": cart_data.get("last_updated"),
                        },
                    }

                    if savings_summary:
                        result["savings_summary"] = savings_summary

                    return result
                except Exception as e:
                    return {"success": False, "error": f"Failed to view cart: {str(e)}"}

            case "add":
                try:
                    if items is not None:
                        is_batch = True
                        if len(items) > 50:
                            return {"success": False, "error": "Maximum 50 items per batch request"}
                        formatted_items = [
                            {
                                "product_id": item["product_id"],
                                "quantity": item.get("quantity", 1),
                                "modality": item.get("modality", "PICKUP"),
                                "description": item.get("description"),
                            }
                            for item in items
                        ]
                    elif product_id:
                        is_batch = False
                        formatted_items = [
                            {
                                "product_id": product_id,
                                "quantity": quantity or 1,
                                "modality": modality or "PICKUP",
                                "description": None,
                            }
                        ]
                    else:
                        return {
                            "success": False,
                            "error": "product_id (single mode) or items (batch mode) is required",
                        }

                    # A manual favorite's synthetic id is not a UPC — Kroger
                    # would reject it, and the whole point of a manual item is
                    # that the user sources it themselves. Caught here rather
                    # than at the API call so batch mode fails before any of
                    # its items are ordered.
                    from ..analytics.favorites import is_manual_product_id

                    manual_ids = [
                        item["product_id"]
                        for item in formatted_items
                        if is_manual_product_id(item["product_id"])
                    ]
                    if manual_ids:
                        return {
                            "success": False,
                            "error": (
                                "These are manual items not sold at Kroger and cannot be "
                                f"added to the cart: {', '.join(manual_ids)}. "
                                "You'll need to source them yourself."
                            ),
                        }

                    invalid_quantities = [
                        item["product_id"]
                        for item in formatted_items
                        if not (1 <= item["quantity"] <= 99)
                    ]
                    if invalid_quantities:
                        return {
                            "success": False,
                            "error": (
                                "quantity must be between 1 and 99 for: "
                                f"{', '.join(invalid_quantities)}"
                            ),
                        }

                    if preview_only:
                        pids = [item["product_id"] for item in formatted_items]
                        pantry_context = {}
                        try:
                            from ..analytics.pantry import get_pantry_item

                            for pid in pids:
                                pantry_item = get_pantry_item(pid, user_id=user_id)
                                if pantry_item:
                                    pantry_context[pid] = {
                                        "level_percent": pantry_item.get("level_percent", 0),
                                        "status": pantry_item.get("status"),
                                        "days_until_empty": pantry_item.get("days_until_empty"),
                                    }
                        except Exception:
                            pass

                        preview_items = []
                        skip_suggestions = []
                        for item in formatted_items:
                            pid = item["product_id"]
                            pantry = pantry_context.get(pid, {})
                            level = pantry.get("level_percent")
                            preview_item = {
                                **item,
                                "pantry_level": level,
                                "pantry_status": pantry.get("status"),
                            }
                            if level is not None and level >= 30:
                                preview_item["recommendation"] = "SKIP"
                                preview_item["reason"] = f"Pantry at {level}%"
                                skip_suggestions.append(preview_item)
                            else:
                                preview_item["recommendation"] = "ADD"
                            preview_items.append(preview_item)

                        return {
                            "success": True,
                            "preview_only": True,
                            "confirmation_required": True,
                            "items": preview_items,
                            "summary": {
                                "total_items": len(preview_items),
                                "items_to_add": len(
                                    [i for i in preview_items if i["recommendation"] == "ADD"]
                                ),
                                "items_to_skip": len(skip_suggestions),
                            },
                            "skip_suggestions": skip_suggestions,
                            "next_step": "Review and call again with preview_only=False to add",
                        }

                    safety_response = check_cart_items_safety(
                        formatted_items, user_id=user_id, confirm_unsafe=bool(confirm_unsafe)
                    )
                    if safety_response is not None:
                        return safety_response

                    if ctx:
                        await ctx.info(f"Adding {len(formatted_items)} item(s) to cart")

                    client = await asyncio.to_thread(get_authenticated_client, user_id)
                    cart_items = [
                        {
                            "upc": item["product_id"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                        }
                        for item in formatted_items
                    ]

                    if ctx:
                        await ctx.info(f"Calling Kroger API to add {len(cart_items)} item(s)")

                    added_items = []
                    failed_items = []

                    try:
                        await asyncio.to_thread(client.cart.add_to_cart, cart_items)
                        added_items = list(formatted_items)
                    except Exception as batch_err:
                        batch_err_str = str(batch_err)
                        is_400 = "400" in batch_err_str or "Bad Request" in batch_err_str
                        is_401 = "401" in batch_err_str or "Unauthorized" in batch_err_str

                        if is_401:
                            raise  # Let outer handler deal with auth errors

                        if is_400 and len(cart_items) > 1:
                            # Batch failed — fall back to per-item adds so valid items still go through
                            if ctx:
                                await ctx.info(
                                    "Batch add failed (400). Retrying items one at a time..."
                                )
                            for cart_item, fmt_item in zip(
                                cart_items, formatted_items, strict=False
                            ):
                                try:
                                    await asyncio.to_thread(client.cart.add_to_cart, [cart_item])
                                    added_items.append(fmt_item)
                                except Exception as item_err:
                                    item_err_str = str(item_err)
                                    item_detail = None
                                    if hasattr(item_err, "response"):
                                        try:
                                            item_detail = item_err.response.text
                                        except Exception:
                                            pass
                                    failed_items.append(
                                        {
                                            "product_id": fmt_item["product_id"],
                                            "error": item_err_str,
                                            "kroger_response": item_detail,
                                        }
                                    )
                        else:
                            # Single-item 400 or non-400 batch error — re-raise
                            raise

                    if ctx:
                        await ctx.info(
                            f"Kroger API: {len(added_items)} added, {len(failed_items)} failed"
                        )

                    local_tracking_errors = []
                    for item in added_items:
                        try:
                            _add_item_to_local_cart(
                                item["product_id"],
                                item["quantity"],
                                item["modality"],
                                product_details={"description": item.get("description")},
                                user_id=user_id,
                            )
                        except Exception as tracking_err:
                            # The real Kroger order already succeeded above —
                            # a local-tracking failure must never flip this
                            # response to success=False, or a caller retrying
                            # on "failure" would place a duplicate real order.
                            logger.error(
                                f"Local cart tracking failed for {item['product_id']} "
                                f"after a successful Kroger order: {tracking_err}"
                            )
                            local_tracking_errors.append(item["product_id"])

                    if ctx:
                        await ctx.info("Item(s) added to local cart tracking")

                    if is_batch:
                        result = {
                            "success": True,
                            "message": (
                                f"Added {len(added_items)} of {len(formatted_items)} items to cart"
                                if failed_items
                                else f"Successfully added {len(added_items)} items to cart"
                            ),
                            "items_added": len(added_items),
                            "items": added_items,
                            "timestamp": datetime.now().isoformat(),
                            "reminder": "Review your cart in the Kroger app before checkout",
                        }
                        if failed_items:
                            result["items_failed"] = len(failed_items)
                            result["failed_items"] = failed_items
                            result["warning"] = (
                                f"{len(failed_items)} item(s) rejected by Kroger API "
                                "(invalid product ID or not available at this location)"
                            )
                        if local_tracking_errors:
                            result["local_tracking_warning"] = (
                                "Kroger order succeeded, but local cart tracking failed "
                                f"for {len(local_tracking_errors)} item(s): "
                                f"{local_tracking_errors}"
                            )
                        return result
                    else:
                        item = formatted_items[0]
                        result = {
                            "success": True,
                            "message": (
                                f"Successfully added {item['quantity']}x "
                                f"{item['product_id']} to cart"
                            ),
                            "product_id": item["product_id"],
                            "quantity": item["quantity"],
                            "modality": item["modality"],
                            "timestamp": datetime.now().isoformat(),
                        }
                        if local_tracking_errors:
                            result["local_tracking_warning"] = (
                                "Kroger order succeeded, but local cart tracking failed."
                            )
                        return result

                except Exception as e:
                    if ctx:
                        await ctx.error(f"Failed to add item(s) to cart: {str(e)}")
                    error_message = str(e)
                    kroger_response = None
                    if hasattr(e, "response"):
                        try:
                            kroger_response = e.response.text
                        except Exception:
                            pass
                    if "401" in error_message or "Unauthorized" in error_message:
                        return {
                            "success": False,
                            "error": "Authentication failed. Please run auth(action='force_reauth') and try again.",
                            "details": error_message,
                        }
                    elif "400" in error_message or "Bad Request" in error_message:
                        return {
                            "success": False,
                            "error": "Invalid request. The product ID may be invalid or unavailable at your location.",
                            "details": error_message,
                            **({"kroger_response": kroger_response} if kroger_response else {}),
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to add item(s) to cart: {error_message}",
                        }

            case "remove":
                ids_to_remove = (
                    product_ids if product_ids else ([product_id] if product_id else None)
                )
                if not ids_to_remove:
                    return {"success": False, "error": "product_id or product_ids is required"}
                try:
                    cart_data = _load_cart_data(user_id=user_id)
                    current_cart = cart_data.get("current_cart", [])
                    original_count = len(current_cart)
                    ids_set = set(ids_to_remove)

                    if modality:
                        cart_data["current_cart"] = [
                            item
                            for item in current_cart
                            if not (
                                item.get("product_id") in ids_set
                                and item.get("modality") == modality
                            )
                        ]
                    else:
                        cart_data["current_cart"] = [
                            item for item in current_cart if item.get("product_id") not in ids_set
                        ]

                    items_removed = original_count - len(cart_data["current_cart"])
                    if items_removed > 0:
                        cart_data["last_updated"] = datetime.now().isoformat()
                        _save_cart_data(cart_data, user_id=user_id)

                    if len(ids_to_remove) == 1:
                        return {
                            "success": True,
                            "message": f"Removed {items_removed} items from local cart tracking",
                            "items_removed": items_removed,
                            "product_id": ids_to_remove[0],
                            "modality": modality,
                        }
                    return {
                        "success": True,
                        "message": f"Removed {items_removed} items from local cart tracking",
                        "items_removed": items_removed,
                        "product_ids_removed": ids_to_remove,
                        "modality": modality,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to remove from cart: {str(e)}"}

            case "clear":
                try:
                    cart_data = _load_cart_data(user_id=user_id)
                    items_count = len(cart_data.get("current_cart", []))
                    cart_data["current_cart"] = []
                    cart_data["last_updated"] = datetime.now().isoformat()
                    _save_cart_data(cart_data, user_id=user_id)
                    return {
                        "success": True,
                        "message": f"Cleared {items_count} items from local cart tracking",
                        "items_cleared": items_count,
                    }
                except Exception as e:
                    return {"success": False, "error": f"Failed to clear cart: {str(e)}"}

            case "mark_placed":
                try:
                    cart_data = _load_cart_data(user_id=user_id)
                    current_cart = cart_data.get("current_cart", [])

                    if not current_cart:
                        return {
                            "success": False,
                            "error": "No items in current cart to mark as placed",
                        }

                    item_count = len(current_cart)
                    total_quantity = sum(item.get("quantity", 0) for item in current_cart)
                    placed_at = datetime.now().isoformat()

                    analytics_order_id = None
                    try:
                        from ..analytics.purchase_tracker import record_order
                        from ..analytics.statistics import update_all_product_stats

                        analytics_order_id = record_order(
                            current_cart, order_notes, user_id=user_id
                        )
                        pids_in_order = [item.get("product_id") for item in current_cart]
                        update_all_product_stats(pids_in_order, user_id=user_id)
                    except Exception as e:
                        logger.warning("Could not record analytics: %s", e)

                    # Restock pantry for all placed items
                    pantry_restocked = 0
                    try:
                        from ..analytics.pantry import restock_item

                        for item in current_cart:
                            pid = item.get("product_id")
                            if pid:
                                try:
                                    restock_item(product_id=pid, level=100, user_id=user_id)
                                    pantry_restocked += 1
                                except Exception as pe:
                                    logger.warning("Could not restock pantry for %s: %s", pid, pe)
                    except Exception as e:
                        logger.warning("Could not import pantry module: %s", e)

                    cart_data["current_cart"] = []
                    cart_data["last_updated"] = datetime.now().isoformat()
                    _save_cart_data(cart_data, user_id=user_id)

                    return {
                        "success": True,
                        "message": f"Marked order with {item_count} items as placed",
                        "order_id": analytics_order_id,
                        "analytics_order_id": analytics_order_id,
                        "items_placed": item_count,
                        "total_quantity": total_quantity,
                        "placed_at": placed_at,
                        "pantry_restocked": pantry_restocked,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to mark order as placed: {str(e)}",
                    }

            case "view_history":
                try:
                    from ..analytics.purchase_tracker import get_order_history

                    hist_limit = max(1, min(50, limit or 10))
                    all_orders = get_order_history(limit=10_000, user_id=user_id)
                    orders = all_orders[:hist_limit]

                    total_orders = len(all_orders)
                    total_items_all_time = sum(
                        order.get("item_count", 0) for order in all_orders
                    )
                    total_quantity_all_time = sum(
                        order.get("total_quantity", 0) for order in all_orders
                    )

                    return {
                        "success": True,
                        "orders": orders,
                        "showing": len(orders),
                        "summary": {
                            "total_orders": total_orders,
                            "total_items_all_time": total_items_all_time,
                            "total_quantity_all_time": total_quantity_all_time,
                        },
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to view order history: {str(e)}",
                    }

            case "get_context":
                try:
                    from ..analytics.favorites import get_list_items, get_lists
                    from ..analytics.pantry import get_pantry_status

                    threshold = pantry_threshold if pantry_threshold is not None else 30

                    result = {
                        "success": True,
                        "pantry_items": [],
                        "favorite_matches": [],
                        "skip_suggestions": [],
                        "low_inventory_alerts": [],
                        "summary": {},
                    }

                    all_pantry = get_pantry_status(apply_depletion=True, user_id=user_id)

                    if product_ids:
                        product_id_set = set(product_ids)
                        filtered_pantry = [
                            item for item in all_pantry if item["product_id"] in product_id_set
                        ]
                    else:
                        filtered_pantry = all_pantry

                    result["pantry_items"] = filtered_pantry

                    for item in filtered_pantry:
                        level = item.get("level_percent", 0)
                        if level >= threshold:
                            result["skip_suggestions"].append(
                                {
                                    "product_id": item["product_id"],
                                    "description": item.get("description"),
                                    "level_percent": level,
                                    "reason": f"Pantry at {level}% (above {threshold}% threshold)",
                                }
                            )
                        elif level <= 20:
                            result["low_inventory_alerts"].append(
                                {
                                    "product_id": item["product_id"],
                                    "description": item.get("description"),
                                    "level_percent": level,
                                    "days_until_empty": item.get("days_until_empty"),
                                    "urgency": "high" if level <= 10 else "medium",
                                }
                            )

                    all_lists = get_lists(user_id=user_id)
                    for fav_list in all_lists:
                        list_id = fav_list["id"]
                        list_items_result = get_list_items(
                            list_id, include_pantry_status=False, user_id=user_id
                        )
                        if list_items_result.get("success") and list_items_result.get("items"):
                            list_pids = {item["product_id"] for item in list_items_result["items"]}
                            if product_ids:
                                matching_ids = list_pids.intersection(set(product_ids))
                            else:
                                matching_ids = list_pids
                            if matching_ids:
                                result["favorite_matches"].append(
                                    {
                                        "list_id": list_id,
                                        "list_name": fav_list["name"],
                                        "matching_products": list(matching_ids),
                                        "match_count": len(matching_ids),
                                    }
                                )

                    result["summary"] = {
                        "pantry_items_checked": len(filtered_pantry),
                        "items_to_skip": len(result["skip_suggestions"]),
                        "low_inventory_count": len(result["low_inventory_alerts"]),
                        "favorite_list_matches": len(result["favorite_matches"]),
                        "pantry_threshold_used": threshold,
                    }

                    if result["skip_suggestions"]:
                        result["guidance"] = (
                            f"You have {len(result['skip_suggestions'])} items that are "
                            f"well-stocked (>{threshold}%). Consider skipping these. "
                            "Ask the user to confirm before adding to cart."
                        )
                    elif result["low_inventory_alerts"]:
                        result["guidance"] = (
                            f"You have {len(result['low_inventory_alerts'])} items running low. "
                            "These should be prioritized for your next order."
                        )
                    else:
                        result["guidance"] = "No pantry data available for these products."

                    return result

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to get shopping context: {str(e)}",
                        "pantry_items": [],
                        "favorite_matches": [],
                        "skip_suggestions": [],
                        "low_inventory_alerts": [],
                    }

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
