#!/usr/bin/env python3
"""Action coverage spec for the Smart Shopper MCP smoke test.

Owner: Smart Shopper maintainers. Pure data — one entry per `action` of every
registered tool, so `scripts/smoke_mcp.py` can assert it covers the whole
surface the live server reports. Splitting the table out of the runner keeps
each file single-responsibility: this one answers "what is safe to call and
with what", the runner answers "how do we call it".

Modes:
  READ    pure query; invoked for real.
  PREVIEW write-capable but has a non-committing mode (confirm=False,
          preview_only=True, mark_as_viewed=False); invoked in that mode only.
  SKIP    no non-committing mode exists; NOT invoked. The third tuple element
          is the reason, which is reported — a skip is never a pass.
"""

from __future__ import annotations

from typing import Any

READ = "read"
PREVIEW = "preview"
SKIP = "skip"

# Known-good fixtures. The location is the one CLAUDE.md pins for this account;
# the product is a stable staple used as a probe for product-scoped actions.
LOCATION_ID = "03400014"
PROBE_PRODUCT_ID = "0001111015405"  # olive oil

# Placeholders resolved from IDs harvested during the discovery phase. An action
# whose placeholder never resolves is reported as SKIP (no fixture), not FAIL —
# "no recipes exist to fetch" is an empty-store fact, not a broken tool.
P_RECIPE = "$recipe_id"
P_GUIDE = "$guide_id"
P_LIST = "$list_id"
P_PLAN = "$plan_id"
P_GAP = "$gap_id"
P_LISTITEM = "$item_id"
# A product this account has actually bought. Stats/history actions legitimately
# report "no data" for an arbitrary product, which would read as a false FAIL.
P_TRACKED = "$tracked_product_id"

# tool -> action -> (mode, args, reason_if_skipped)
SPEC: dict[str, dict[str, tuple[str, dict[str, Any], str]]] = {
    "auth": {
        "start": (READ, {}, ""),
        "complete": (SKIP, {}, "needs a live OAuth redirect_url with a one-time code"),
        "get_profile": (READ, {}, ""),
        "test": (READ, {}, ""),
        "get_info": (READ, {}, ""),
        "force_reauth": (SKIP, {}, "invalidates the live user token"),
    },
    "cart": {
        "view": (READ, {}, ""),
        "add": (
            PREVIEW,
            {"product_id": PROBE_PRODUCT_ID, "quantity": 1, "preview_only": True},
            "",
        ),
        "remove": (SKIP, {}, "mutates the live cart; no preview mode"),
        "clear": (SKIP, {}, "destroys the live cart"),
        "mark_placed": (SKIP, {}, "records a real order against history"),
        "view_history": (READ, {}, ""),
        "get_context": (READ, {}, ""),
    },
    "deals": {
        "find": (READ, {"limit": 3}, ""),
        "add_to_watchlist": (SKIP, {}, "persists a watchlist row; no preview mode"),
        "get_price_history": (READ, {"product_id": P_TRACKED}, ""),
        "score_quality": (READ, {"product_id": PROBE_PRODUCT_ID}, ""),
        "scan_watchlist": (READ, {"mark_as_viewed": False}, ""),
        "get_latest_scan": (READ, {}, ""),
    },
    "favorites": {
        "create_list": (SKIP, {}, "creates a persistent list"),
        "get_lists": (READ, {}, ""),
        "rename_list": (SKIP, {}, "mutates a live list"),
        "delete_list": (SKIP, {}, "destroys a live list"),
        "add_item": (SKIP, {}, "mutates a live list"),
        "remove_item": (SKIP, {}, "mutates a live list"),
        "get_items": (READ, {"list_id": P_LIST}, ""),
        "order": (PREVIEW, {"list_id": P_LIST, "confirm": False}, ""),
        "suggest": (READ, {}, ""),
        "update_schedule": (SKIP, {}, "mutates list scheduling"),
        "set_stock_level": (SKIP, {}, "mutates stock state"),
        "update_quantity": (SKIP, {}, "mutates list quantities"),
        "get_low_stock": (READ, {}, ""),
        "check_snacks": (READ, {}, ""),
    },
    "guides": {
        "list": (READ, {}, ""),
        "get": (READ, {"guide_id": P_GUIDE}, ""),
        "save": (SKIP, {}, "persists a guide"),
        "update": (SKIP, {}, "mutates a stored guide"),
        "delete": (SKIP, {}, "destroys a stored guide"),
        "search": (READ, {"query": "chicken"}, ""),
    },
    "info": {
        "list_chains": (READ, {}, ""),
        "get_chain": (READ, {"chain_name": "Kroger"}, ""),
        "check_chain": (READ, {"chain_name": "Kroger"}, ""),
        "list_departments": (READ, {}, ""),
        "get_department": (READ, {"department_id": "01"}, ""),
        "check_department": (READ, {"department_id": "01"}, ""),
        "get_datetime": (READ, {}, ""),
        "get_servings": (READ, {}, ""),
        "set_servings": (SKIP, {}, "mutates the stored servings preference"),
        "get_preferences": (READ, {}, ""),
    },
    "ingredients": {
        "add_custom": (SKIP, {}, "persists a custom ingredient rule"),
        "edit_custom": (SKIP, {}, "mutates a custom rule"),
        "remove_custom": (SKIP, {}, "destroys a custom rule"),
        "list_custom": (READ, {}, ""),
        "override_system": (SKIP, {}, "mutates the system ingredient table"),
        "reset_to_default": (SKIP, {}, "wipes all custom ingredient rules"),
        "get_info": (READ, {"ingredient_name": "high fructose corn syrup"}, ""),
        "import_list": (SKIP, {}, "bulk-writes ingredient rules"),
        "export_list": (READ, {}, ""),
        "preview_impact": (
            READ,
            {"ingredient_name": "aspartame", "severity": "warning"},
            "",
        ),
    },
    "location": {
        "search": (READ, {"zip_code": "77301", "limit": 3}, ""),
        "get_details": (READ, {"location_id": LOCATION_ID}, ""),
        "set_preferred": (SKIP, {}, "mutates the stored preferred location"),
        "get_preferred": (READ, {}, ""),
        "check_exists": (READ, {"location_id": LOCATION_ID}, ""),
        "get_zip": (READ, {}, ""),
    },
    "meal_plan": {
        "create": (SKIP, {}, "persists a meal plan"),
        "list": (READ, {}, ""),
        "get": (READ, {"plan_id": P_PLAN}, ""),
        "update": (SKIP, {}, "mutates a stored plan"),
        "delete": (SKIP, {}, "destroys a stored plan"),
        "copy": (SKIP, {}, "persists a new plan"),
        "assign_meal": (SKIP, {}, "mutates plan contents"),
        "remove_meal": (SKIP, {}, "mutates plan contents"),
        "swap": (SKIP, {}, "mutates plan contents"),
        "mark_cooked": (SKIP, {}, "records cooking history and deducts pantry"),
        "preview_shopping": (READ, {"plan_id": P_PLAN}, ""),
        "add_to_cart": (PREVIEW, {"plan_id": P_PLAN, "confirm": False}, ""),
        "get_week_view": (READ, {}, ""),
        "get_summary": (READ, {"plan_id": P_PLAN}, ""),
    },
    "notion": {
        "setup": (SKIP, {}, "writes Notion database schema"),
        "sync_all": (SKIP, {}, "pushes every recipe to Notion"),
        "pull_changes": (SKIP, {}, "overwrites local recipes from Notion"),
        "update_tags": (SKIP, {}, "mutates recipe tags"),
        "bulk_tag": (SKIP, {}, "mutates recipe tags in bulk"),
        "get_status": (READ, {}, ""),
        "view_recipe": (READ, {"recipe_id": P_RECIPE}, ""),
    },
    "pantry": {
        "get": (READ, {}, ""),
        "add": (SKIP, {}, "persists a pantry item"),
        "update_item": (SKIP, {}, "mutates pantry levels"),
        "restock": (SKIP, {}, "mutates pantry levels"),
        "get_low_inventory": (READ, {}, ""),
        "remove": (SKIP, {}, "destroys a pantry item"),
        "get_attention": (READ, {}, ""),
        "list_gaps": (READ, {}, ""),
        "resolve_gap": (SKIP, {}, "mutates gap state"),
    },
    "predictions": {
        "get_predictions": (READ, {}, ""),
        "get_item_stats": (READ, {"product_id": P_TRACKED}, ""),
        "categorize": (SKIP, {}, "writes the stored routine/regular/treat category"),
        "get_by_category": (READ, {"category": "routine"}, ""),
        "get_history": (READ, {"product_id": P_TRACKED}, ""),
        "get_suggestions": (READ, {}, ""),
        "get_smart_recommendations": (READ, {}, ""),
        "explain_recommendation": (READ, {"product_id": P_TRACKED}, ""),
        "get_seasonal": (READ, {}, ""),
        "get_upcoming_holidays": (READ, {}, ""),
        "migrate_data": (SKIP, {}, "rewrites the analytics store"),
        "get_category_summary": (READ, {}, ""),
        "configure": (SKIP, {}, "mutates prediction tuning config"),
        "get_config": (READ, {}, ""),
        "reset_config": (SKIP, {}, "wipes prediction tuning config"),
    },
    "privacy": {
        "get_consent": (READ, {}, ""),
        "set_consent": (SKIP, {}, "mutates stored consent"),
        "withdraw": (SKIP, {}, "withdraws consent"),
        "delete_my_data": (SKIP, {}, "irreversibly deletes all user data"),
    },
    "products": {
        "search": (READ, {"search_term": "olive oil", "limit": 3}, ""),
        "get_details": (READ, {"product_id": PROBE_PRODUCT_ID}, ""),
        "get_images": (READ, {"product_id": PROBE_PRODUCT_ID}, ""),
        "search_by_id": (READ, {"product_id": PROBE_PRODUCT_ID}, ""),
        "add_to_whole_foods": (SKIP, {}, "persists a catalog entry"),
        "get_whole_foods_catalog": (READ, {}, ""),
        "scan_for_whole_foods": (
            READ,
            {"category": "produce", "auto_add": False, "limit": 3},
            "",
        ),
    },
    "recipes": {
        "save": (SKIP, {}, "persists a recipe"),
        "list": (READ, {}, ""),
        "get": (READ, {"recipe_id": P_RECIPE}, ""),
        "update": (SKIP, {}, "mutates a stored recipe"),
        "delete": (SKIP, {}, "destroys a stored recipe"),
        "search": (READ, {"query": "chicken"}, ""),
        "preview_order": (READ, {"recipe_id": P_RECIPE}, ""),
        "link_ingredient": (SKIP, {}, "mutates recipe ingredient links"),
        "add_to_cart": (PREVIEW, {"recipe_id": P_RECIPE, "confirm": False}, ""),
        "analyze": (READ, {"recipe_id": P_RECIPE}, ""),
    },
    "reports": {
        "get_analytics": (READ, {"report_type": "spending"}, ""),
        "export_data": (READ, {"report_type": "spending"}, ""),
        "check_recipe_pantry": (READ, {"recipe_id": P_RECIPE}, ""),
        "generate_shopping_list": (READ, {"recipe_ids": [P_RECIPE]}, ""),
        "get_cookable_recipes": (READ, {}, ""),
    },
    "safety": {
        "get_settings": (READ, {}, ""),
        "configure": (SKIP, {}, "mutates safety filtering settings"),
        "get_bad_ingredients": (READ, {}, ""),
        "toggle_ingredient": (SKIP, {}, "mutates ingredient enablement"),
        "get_preferences": (READ, {}, ""),
        "reset_preferences": (SKIP, {}, "wipes safety preferences"),
        "approve_product": (SKIP, {}, "persists an approval"),
        "unapprove_product": (SKIP, {}, "mutates the approved list"),
        "get_safe_products": (READ, {}, ""),
        "block_product": (SKIP, {}, "persists a block"),
        "unblock_product": (SKIP, {}, "mutates the blocked list"),
        "get_blocked_products": (READ, {}, ""),
        "check_product": (
            READ,
            {"product_id": PROBE_PRODUCT_ID, "description": "Extra Virgin Olive Oil"},
            "",
        ),
        "check_products": (
            READ,
            {
                "products": [
                    {
                        "product_id": PROBE_PRODUCT_ID,
                        "description": "Extra Virgin Olive Oil",
                    }
                ]
            },
            "",
        ),
        "check_cart": (READ, {}, ""),
    },
    "shopping_list": {
        "add_recipe": (SKIP, {}, "persists list items"),
        "get": (READ, {}, ""),
        "remove": (SKIP, {}, "destroys a list item"),
        "update_item": (SKIP, {}, "mutates a list item"),
        "add_to_cart": (PREVIEW, {"confirm": False}, ""),
    },
}

# Which discovery probe supplies each placeholder: (tool, action, keys to search).
FIXTURE_PROBES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("recipes", "list", P_RECIPE, ("recipe_id", "id")),
    ("guides", "list", P_GUIDE, ("guide_id", "id")),
    ("favorites", "get_lists", P_LIST, ("list_id", "id")),
    ("meal_plan", "list", P_PLAN, ("plan_id", "id")),
    ("pantry", "list_gaps", P_GAP, ("gap_id", "id")),
    ("shopping_list", "get", P_LISTITEM, ("item_id", "id")),
    ("predictions", "get_predictions", P_TRACKED, ("product_id",)),
]
