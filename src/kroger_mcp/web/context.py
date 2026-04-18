"""
Action-menu template context loader.

OWNS: assembling the target-entity bundle (favorite lists, recipes, meal plans)
that the unified action-menu dropdown offers as submenu choices, plus the
accompanying Jinja snippet that serializes it into a page-level JSON data
island the Alpine store reads on init.

DOES NOT OWN:
  - menu rendering (see templates/_macros/action_menu.html)
  - interaction state or network calls (see static/js/action_menu.js and the
    host page's Alpine component)

CALLED BY: page route handlers that render templates containing action menus —
routes/products.py, routes/deals.py, routes/favorites.py, routes/recipes.py,
routes/shopping_list.py.

CALLS: analytics.favorites.get_lists, tools.recipe_tools._load_recipes,
analytics.meal_planning.list_plans_for_api.

# @stable — the returned dict's shape is the contract the Jinja macro and
# Alpine store depend on. Changing keys requires coordinated updates in
# templates/_macros/action_menu.html and static/js/action_menu.js.
"""
from typing import Any, Dict, List


def action_menu_context() -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Build the target-entity bundle for action-menu submenus.

    Returns a single top-level key 'action_menu_data' whose value is a dict
    with three lists the Jinja layer serializes into a JSON data island that
    Alpine.store('actionMenu') reads on page init:
      - favoritesLists: user-created lists. The default list is excluded
        because it is a Kroger-side reorder target, not a human-chosen save
        destination — surfacing it as an equal choice misleads the user.
      - recipes: id + name only; enough for the "Add to Recipe" submenu to
        identify each recipe and dispatch an ingredient-append event.
      - mealPlans: non-template plans only. Templates are structural and not
        valid drop targets for a single recipe assignment.

    Key is named 'action_menu_data' (not 'action_menu') to avoid colliding
    with the Jinja macro `action_menu` that templates import from
    _macros/action_menu.html. Callers: `context = {..., **action_menu_context()}`.

    Source failures degrade to empty lists (logged) instead of raising, because
    a missing data source must not block an otherwise-renderable page — the
    menu just shows "no options" for that branch. This is the one swallow the
    project tolerates; see individual _load_* helpers for the rationale.

    Keys inside the bundle are camelCase because the JSON is consumed by
    JavaScript and base.html serializes this dict directly via |tojson.
    """
    return {
        "action_menu_data": {
            "favoritesLists": _load_favorites_lists(),
            "recipes": _load_recipe_choices(),
            "mealPlans": _load_meal_plan_choices(),
        }
    }


def _load_favorites_lists() -> List[Dict[str, Any]]:
    # Counterpart: see routes/api/favorites.py::get_favorites_lists —
    # identical filter (exclude is_default) so the menu choices match the
    # dedicated API endpoint the host Alpine components refetch after
    # creating a new list.
    try:
        from kroger_mcp.analytics.favorites import get_lists
        return [
            {"id": lst["id"], "name": lst["name"]}
            for lst in get_lists()
            if not lst.get("is_default")
        ]
    except Exception as exc:
        print(f"[action_menu_context] favorites lists unavailable: {exc}")
        return []


def _load_recipe_choices() -> List[Dict[str, Any]]:
    try:
        from kroger_mcp.tools.recipe_tools import _load_recipes
        return [
            {"id": r["id"], "name": r.get("name") or r["id"]}
            for r in _load_recipes().get("recipes", [])
            if r.get("id")
        ]
    except Exception as exc:
        print(f"[action_menu_context] recipes unavailable: {exc}")
        return []


def _load_meal_plan_choices() -> List[Dict[str, Any]]:
    try:
        from kroger_mcp.analytics.meal_planning import list_plans_for_api
        outcome = list_plans_for_api(include_templates=False, limit=50)
        plans = outcome.get("plans", []) if outcome.get("success") else []
        return [
            {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "start_date": p.get("start_date"),
            }
            for p in plans
        ]
    except Exception as exc:
        print(f"[action_menu_context] meal plans unavailable: {exc}")
        return []
