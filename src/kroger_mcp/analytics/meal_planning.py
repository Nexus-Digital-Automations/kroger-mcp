"""
Meal planning business logic.

Provides functions for:
- Creating and managing meal plans
- Assigning recipes to meal slots
- Generating shopping lists for meal plans
- Checking pantry availability for meal plans
"""

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from kroger_mcp.auth.dependencies import mcp_user_id

from .database import ensure_initialized, get_db_connection, get_db_cursor
from .pantry import get_pantry_status
from .recipe_integration import match_ingredient_to_pantry


def _resolve_user_id(user_id: str | None) -> str:
    """Resolve user_id for user-scoped queries.

    HTTP route handlers always pass user_id from the session. MCP/script
    callers may pass None; we fall back to `mcp_user_id()` which honors
    KROGER_MCP_USER_ID per Claude Desktop profile, then
    KROGER_MCP_DEFAULT_USER_ID. This means MCP profiles bound to different
    users see only their own data — no per-tool-dispatcher threading needed.
    """
    return user_id if user_id is not None else mcp_user_id()


VALID_MEAL_SLOTS = {"breakfast", "lunch", "dinner", "snack"}
VALID_PLAN_TYPES = {"weekly", "monthly", "custom"}
RECIPES_FILE = "kroger_recipes.json"


def _parse_date(date_str: str) -> datetime:
    """Parse a YYYY-MM-DD date string."""
    return datetime.strptime(date_str, "%Y-%m-%d")


def _safe_float(value: Any, default: float = 1.0) -> float:
    """Convert a quantity value to float, returning default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_date(dt: datetime) -> str:
    """Format a datetime as YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def _get_recipe_from_json(recipe_id: str) -> dict[str, Any] | None:
    """Get recipe from JSON file (primary storage)."""
    try:
        if os.path.exists(RECIPES_FILE):
            with open(RECIPES_FILE) as f:
                data = json.load(f)
                for recipe in data.get("recipes", []):
                    if recipe.get("id") == recipe_id:
                        return recipe
    except Exception:
        pass
    return None


def _get_recipe_from_db(recipe_id: str) -> dict[str, Any] | None:
    """Get recipe from SQLite database."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
        row = cursor.fetchone()
        if not row:
            return None

        recipe = dict(row)

        # Get ingredients
        cursor = conn.execute("SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
        recipe["ingredients"] = [dict(r) for r in cursor.fetchall()]

        return recipe
    finally:
        conn.close()


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    """Get recipe from either JSON or database."""
    # Try JSON first (primary storage)
    recipe = _get_recipe_from_json(recipe_id)
    if recipe:
        return recipe

    # Fallback to database
    return _get_recipe_from_db(recipe_id)


# ============== Meal Plan CRUD ==============


def create_meal_plan(
    name: str,
    start_date: str,
    end_date: str | None = None,
    plan_type: str = "weekly",
    description: str | None = None,
    is_template: bool = False,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Create a new meal plan owned by `user_id`.

    Args:
        name: Plan name (e.g., "Week of Jan 27")
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD (defaults to start + 6 days for weekly)
        plan_type: 'weekly', 'monthly', or 'custom'
        description: Optional description
        is_template: Whether this is a reusable template
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Created plan info with plan_id
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if plan_type not in VALID_PLAN_TYPES:
        return {"success": False, "error": f"Invalid plan_type. Must be one of: {VALID_PLAN_TYPES}"}

    try:
        start_dt = _parse_date(start_date)
    except ValueError:
        return {"success": False, "error": "Invalid start_date format. Use YYYY-MM-DD"}

    # Default end_date based on plan_type
    if not end_date:
        if plan_type == "weekly":
            end_dt = start_dt + timedelta(days=6)
        elif plan_type == "monthly":
            # Roughly 30 days
            end_dt = start_dt + timedelta(days=29)
        else:
            end_dt = start_dt + timedelta(days=6)
        end_date = _format_date(end_dt)
    else:
        try:
            end_dt = _parse_date(end_date)
        except ValueError:
            return {"success": False, "error": "Invalid end_date format. Use YYYY-MM-DD"}

    if end_dt < start_dt:
        return {"success": False, "error": "end_date must be on or after start_date"}

    plan_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    days_count = (end_dt - start_dt).days + 1

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO meal_plans
            (id, name, description, start_date, end_date, plan_type,
             is_template, created_at, updated_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                plan_id,
                name,
                description,
                start_date,
                end_date,
                plan_type,
                int(is_template),
                now,
                now,
                owner,
            ),
        )

    return {
        "success": True,
        "plan_id": plan_id,
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "days_count": days_count,
        "plan_type": plan_type,
        "is_template": is_template,
        "message": f"Created meal plan '{name}' covering {days_count} days",
    }


def get_meal_plans(
    include_past: bool = False,
    include_templates: bool = False,
    limit: int = 20,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    List meal plans owned by `user_id` with summary info.

    Args:
        include_past: Include plans with end_date before today
        include_templates: Include template plans
        limit: Maximum number of plans to return
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        List of plan summaries
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        today = _format_date(datetime.now())

        query = "SELECT * FROM meal_plans WHERE user_id = ?"
        params: list[Any] = [owner]

        if not include_past:
            query += " AND end_date >= ?"
            params.append(today)

        if not include_templates:
            query += " AND is_template = 0"

        query += " ORDER BY start_date DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(query, params)
        plans = [dict(row) for row in cursor.fetchall()]

        # Get meal counts for each plan (entries scoped to owner via the plan FK + WHERE)
        for plan in plans:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM meal_entries WHERE plan_id = ? AND user_id = ?",
                (plan["id"], owner),
            )
            plan["meal_count"] = cursor.fetchone()[0]
            plan["is_template"] = bool(plan.get("is_template"))

        # Count templates and upcoming for this user
        cursor = conn.execute(
            "SELECT COUNT(*) FROM meal_plans WHERE user_id = ? AND is_template = 1", (owner,)
        )
        template_count = cursor.fetchone()[0]

        cursor = conn.execute(
            "SELECT COUNT(*) FROM meal_plans "
            "WHERE user_id = ? AND end_date >= ? AND is_template = 0",
            (owner, today),
        )
        upcoming_count = cursor.fetchone()[0]

        return {
            "success": True,
            "plans": plans,
            "total_count": len(plans),
            "upcoming_count": upcoming_count,
            "template_count": template_count,
        }
    finally:
        conn.close()


def get_meal_plan(
    plan_id: str,
    include_recipe_details: bool = True,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Get full details of a meal plan owned by `user_id`, including all meal entries.

    Args:
        plan_id: Plan identifier
        include_recipe_details: Whether to fetch full recipe info
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Plan with meals_by_date and recipe_summary
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        # Get plan
        cursor = conn.execute(
            "SELECT * FROM meal_plans WHERE id = ? AND user_id = ?", (plan_id, owner)
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Meal plan '{plan_id}' not found"}

        plan = dict(row)
        plan["is_template"] = bool(plan.get("is_template"))

        # Get meal entries
        cursor = conn.execute(
            """
            SELECT * FROM meal_entries
            WHERE plan_id = ? AND user_id = ?
            ORDER BY meal_date, meal_slot
        """,
            (plan_id, owner),
        )
        entries = [dict(r) for r in cursor.fetchall()]

        # Organize by date
        meals_by_date: dict[str, dict[str, Any]] = {}
        recipe_ids = set()

        for entry in entries:
            date = entry["meal_date"]
            slot = entry["meal_slot"]

            if date not in meals_by_date:
                meals_by_date[date] = {}

            meal_info = {
                "recipe_id": entry["recipe_id"],
                "servings_override": entry.get("servings_override"),
                "notes": entry.get("notes"),
            }

            if include_recipe_details:
                recipe = get_recipe(entry["recipe_id"])
                if recipe:
                    meal_info["recipe_name"] = recipe.get("name")
                    meal_info["recipe_servings"] = recipe.get("servings", 4)
                    recipe_ids.add(entry["recipe_id"])

            meals_by_date[date][slot] = meal_info

        # Recipe summary
        recipe_summary = []
        recipe_counts: dict[str, int] = {}
        for entry in entries:
            rid = entry["recipe_id"]
            recipe_counts[rid] = recipe_counts.get(rid, 0) + 1

        for rid, count in recipe_counts.items():
            recipe = get_recipe(rid)
            recipe_summary.append(
                {
                    "recipe_id": rid,
                    "recipe_name": recipe.get("name") if recipe else rid,
                    "times_used": count,
                }
            )

        return {
            "success": True,
            "plan": plan,
            "meals_by_date": meals_by_date,
            "meal_count": len(entries),
            "recipe_summary": recipe_summary,
            "unique_recipes": len(recipe_ids),
        }
    finally:
        conn.close()


def update_meal_plan(
    plan_id: str,
    name: str | None = None,
    description: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Update meal plan metadata, only if `user_id` owns the plan.

    Args:
        plan_id: Plan identifier
        name: New name (optional)
        description: New description (optional)
        start_date: New start date (optional)
        end_date: New end date (optional)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Updated plan info
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        # Check plan exists and is owned by this user
        cursor = conn.execute(
            "SELECT * FROM meal_plans WHERE id = ? AND user_id = ?", (plan_id, owner)
        )
        if not cursor.fetchone():
            return {"success": False, "error": f"Meal plan '{plan_id}' not found"}

        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if start_date is not None:
            try:
                _parse_date(start_date)
            except ValueError:
                return {"success": False, "error": "Invalid start_date format"}
            updates.append("start_date = ?")
            params.append(start_date)
        if end_date is not None:
            try:
                _parse_date(end_date)
            except ValueError:
                return {"success": False, "error": "Invalid end_date format"}
            updates.append("end_date = ?")
            params.append(end_date)

        if not updates:
            return {"success": False, "error": "No fields to update"}

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(plan_id)
        params.append(owner)

        conn.execute(
            f"UPDATE meal_plans SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        conn.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "message": "Meal plan updated",
            "fields_updated": len(updates) - 1,  # Exclude updated_at
        }
    finally:
        conn.close()


def delete_meal_plan(plan_id: str, user_id: str | None = None) -> dict[str, Any]:
    """
    Delete a meal plan and all its meal entries, only if `user_id` owns the plan.

    Args:
        plan_id: Plan identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Confirmation of deletion
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        # Check plan exists and is owned by this user
        cursor = conn.execute(
            "SELECT name FROM meal_plans WHERE id = ? AND user_id = ?", (plan_id, owner)
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Meal plan '{plan_id}' not found"}

        plan_name = row[0]

        # Get meal count before delete
        cursor = conn.execute(
            "SELECT COUNT(*) FROM meal_entries WHERE plan_id = ? AND user_id = ?",
            (plan_id, owner),
        )
        meal_count = cursor.fetchone()[0]

        # Delete (CASCADE removes meal_entries via plan_id FK)
        conn.execute("DELETE FROM meal_plans WHERE id = ? AND user_id = ?", (plan_id, owner))
        conn.commit()

        return {
            "success": True,
            "message": f"Deleted meal plan '{plan_name}'",
            "meals_removed": meal_count,
        }
    finally:
        conn.close()


def copy_meal_plan(
    source_plan_id: str,
    new_name: str,
    new_start_date: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Copy a meal plan to a new date range, only if `user_id` owns the source plan.

    All meals are shifted to the new date range maintaining their
    relative positions (day offset and meal slot).

    Args:
        source_plan_id: Plan to copy from
        new_name: Name for the new plan
        new_start_date: Start date for the new plan YYYY-MM-DD
        user_id: Owner of source and destination plans.

    Returns:
        New plan info
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    # Get source plan (scoped to owner)
    source = get_meal_plan(source_plan_id, include_recipe_details=False, user_id=owner)
    if not source.get("success"):
        return source

    plan = source["plan"]
    source_start = _parse_date(plan["start_date"])
    source_end = _parse_date(plan["end_date"])
    duration = (source_end - source_start).days

    try:
        new_start_dt = _parse_date(new_start_date)
    except ValueError:
        return {"success": False, "error": "Invalid new_start_date format. Use YYYY-MM-DD"}

    new_end_dt = new_start_dt + timedelta(days=duration)
    new_end_date = _format_date(new_end_dt)

    # Create new plan owned by the same user
    result = create_meal_plan(
        name=new_name,
        start_date=new_start_date,
        end_date=new_end_date,
        plan_type=plan["plan_type"],
        description=plan.get("description"),
        is_template=False,
        user_id=owner,
    )

    if not result.get("success"):
        return result

    new_plan_id = result["plan_id"]

    # Copy meal entries with date offset
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM meal_entries WHERE plan_id = ? AND user_id = ?",
            (source_plan_id, owner),
        )
        entries = [dict(r) for r in cursor.fetchall()]

        copied = 0
        for entry in entries:
            old_date = _parse_date(entry["meal_date"])
            offset = (old_date - source_start).days
            new_date = _format_date(new_start_dt + timedelta(days=offset))

            conn.execute(
                """
                INSERT INTO meal_entries
                (plan_id, recipe_id, meal_date, meal_slot,
                 servings_override, notes, created_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    new_plan_id,
                    entry["recipe_id"],
                    new_date,
                    entry["meal_slot"],
                    entry.get("servings_override"),
                    entry.get("notes"),
                    datetime.now().isoformat(),
                    owner,
                ),
            )
            copied += 1

        conn.commit()

        result["meals_copied"] = copied
        result["message"] = f"Copied plan with {copied} meals"
        return result
    finally:
        conn.close()


# ============== Meal Assignment ==============


def assign_meal(
    plan_id: str,
    recipe_id: str,
    meal_date: str,
    meal_slot: str,
    servings_override: int | None = None,
    notes: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Assign a recipe to a specific day and meal slot, only if `user_id`
    owns the target plan.

    Replaces any existing recipe in that slot.

    If servings_override is None, uses the user's default_servings_per_meal
    preference instead of the recipe's base servings. This ensures meals
    are automatically scaled to household size.

    Args:
        plan_id: Plan identifier
        recipe_id: Recipe to assign
        meal_date: Date YYYY-MM-DD
        meal_slot: 'breakfast', 'lunch', 'dinner', or 'snack'
        servings_override: Override servings (None = use household default)
        notes: Optional notes
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Confirmation of assignment with servings information
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if meal_slot not in VALID_MEAL_SLOTS:
        return {"success": False, "error": f"Invalid meal_slot. Must be one of: {VALID_MEAL_SLOTS}"}

    try:
        meal_dt = _parse_date(meal_date)
    except ValueError:
        return {"success": False, "error": "Invalid meal_date format. Use YYYY-MM-DD"}

    # Get household default servings if not overridden
    from ..tools.shared import get_default_servings

    household_default = get_default_servings()

    if servings_override is None:
        servings_override = household_default
        servings_source = "household_default"
    else:
        servings_source = "explicit_override"

    conn = get_db_connection()
    try:
        # Verify plan exists, owned by this user, and date is in range
        cursor = conn.execute(
            "SELECT start_date, end_date FROM meal_plans WHERE id = ? AND user_id = ?",
            (plan_id, owner),
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Meal plan '{plan_id}' not found"}

        start_dt = _parse_date(row[0])
        end_dt = _parse_date(row[1])

        if not (start_dt <= meal_dt <= end_dt):
            return {"success": False, "error": f"meal_date must be between {row[0]} and {row[1]}"}

        # Verify recipe exists (recipes are JSON-stored; scoping handled there)
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

        # Insert or replace (UNIQUE(plan_id, meal_date, meal_slot) handles this)
        conn.execute(
            """
            INSERT OR REPLACE INTO meal_entries
            (plan_id, recipe_id, meal_date, meal_slot,
             servings_override, notes, created_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                plan_id,
                recipe_id,
                meal_date,
                meal_slot,
                servings_override,
                notes,
                datetime.now().isoformat(),
                owner,
            ),
        )
        conn.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "meal_date": meal_date,
            "meal_slot": meal_slot,
            "recipe_id": recipe_id,
            "recipe_name": recipe.get("name"),
            "servings": servings_override,
            "servings_source": servings_source,
            "household_default": household_default,
            "recipe_base_servings": recipe.get("servings", 4),
            "message": f"Assigned '{recipe.get('name')}' to {meal_slot} on {meal_date} ({servings_override} servings from {servings_source})",
        }
    finally:
        conn.close()


def remove_meal(
    plan_id: str, meal_date: str, meal_slot: str, user_id: str | None = None
) -> dict[str, Any]:
    """
    Remove a recipe from a meal slot, only if `user_id` owns the plan.

    Args:
        plan_id: Plan identifier
        meal_date: Date YYYY-MM-DD
        meal_slot: 'breakfast', 'lunch', 'dinner', or 'snack'
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Confirmation of removal
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if meal_slot not in VALID_MEAL_SLOTS:
        return {"success": False, "error": f"Invalid meal_slot. Must be one of: {VALID_MEAL_SLOTS}"}

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            DELETE FROM meal_entries
            WHERE plan_id = ? AND meal_date = ? AND meal_slot = ? AND user_id = ?
        """,
            (plan_id, meal_date, meal_slot, owner),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return {"success": False, "error": f"No meal found at {meal_slot} on {meal_date}"}

        return {"success": True, "message": f"Removed {meal_slot} on {meal_date}"}
    finally:
        conn.close()


def swap_meals(
    plan_id: str,
    date1: str,
    slot1: str,
    date2: str,
    slot2: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Swap two meal assignments within the same plan, only if `user_id` owns it.

    Args:
        plan_id: Plan identifier
        date1, slot1: First meal
        date2, slot2: Second meal
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Confirmation of swap
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    for slot in [slot1, slot2]:
        if slot not in VALID_MEAL_SLOTS:
            return {
                "success": False,
                "error": f"Invalid meal_slot '{slot}'. Must be one of: {VALID_MEAL_SLOTS}",
            }

    conn = get_db_connection()
    try:
        # Get both entries (owner-scoped so we never read other users' rows)
        cursor = conn.execute(
            """
            SELECT meal_date, meal_slot, recipe_id, servings_override, notes
            FROM meal_entries
            WHERE plan_id = ? AND user_id = ? AND
                  ((meal_date = ? AND meal_slot = ?) OR
                   (meal_date = ? AND meal_slot = ?))
        """,
            (plan_id, owner, date1, slot1, date2, slot2),
        )

        entries = {(r[0], r[1]): r for r in cursor.fetchall()}

        entry1 = entries.get((date1, slot1))
        entry2 = entries.get((date2, slot2))

        if not entry1 and not entry2:
            return {"success": False, "error": "Neither meal slot has an assignment"}

        # Delete both (owner-scoped)
        conn.execute(
            """
            DELETE FROM meal_entries
            WHERE plan_id = ? AND user_id = ? AND
                  ((meal_date = ? AND meal_slot = ?) OR
                   (meal_date = ? AND meal_slot = ?))
        """,
            (plan_id, owner, date1, slot1, date2, slot2),
        )

        # Re-insert swapped
        now = datetime.now().isoformat()

        if entry1:
            conn.execute(
                """
                INSERT INTO meal_entries
                (plan_id, recipe_id, meal_date, meal_slot,
                 servings_override, notes, created_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (plan_id, entry1[2], date2, slot2, entry1[3], entry1[4], now, owner),
            )

        if entry2:
            conn.execute(
                """
                INSERT INTO meal_entries
                (plan_id, recipe_id, meal_date, meal_slot,
                 servings_override, notes, created_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (plan_id, entry2[2], date1, slot1, entry2[3], entry2[4], now, owner),
            )

        conn.commit()

        return {"success": True, "message": f"Swapped {slot1} on {date1} with {slot2} on {date2}"}
    finally:
        conn.close()


def bulk_assign_meals(
    plan_id: str, assignments: list[dict[str, Any]], user_id: str | None = None
) -> dict[str, Any]:
    """
    Assign multiple meals at once, only if `user_id` owns the plan.

    Args:
        plan_id: Plan identifier
        assignments: List of dicts with recipe_id, meal_date, meal_slot,
                    and optional servings_override
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Summary of assignments
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if not assignments:
        return {"success": False, "error": "No assignments provided"}

    results = {"success": True, "assigned": 0, "failed": 0, "errors": []}

    for assignment in assignments:
        result = assign_meal(
            plan_id=plan_id,
            recipe_id=assignment.get("recipe_id", ""),
            meal_date=assignment.get("meal_date", ""),
            meal_slot=assignment.get("meal_slot", ""),
            servings_override=assignment.get("servings_override"),
            notes=assignment.get("notes"),
            user_id=owner,
        )

        if result.get("success"):
            results["assigned"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({"assignment": assignment, "error": result.get("error")})

    results["message"] = f"Assigned {results['assigned']} meals"
    if results["failed"] > 0:
        results["message"] += f", {results['failed']} failed"

    return results


# ============== Shopping Integration ==============


def get_meal_entries_for_dates(
    plan_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Get meal entries for `user_id` in a date range (optionally filtered by plan).

    Args:
        plan_id: Optional plan to filter by
        start_date: Start of date range YYYY-MM-DD
        end_date: End of date range YYYY-MM-DD
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        List of meal entries with recipe info
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        query = """
            SELECT me.*, mp.name as plan_name
            FROM meal_entries me
            JOIN meal_plans mp ON me.plan_id = mp.id
            WHERE me.user_id = ?
        """
        params: list[Any] = [owner]

        if plan_id:
            query += " AND me.plan_id = ?"
            params.append(plan_id)

        if start_date:
            query += " AND me.meal_date >= ?"
            params.append(start_date)

        if end_date:
            query += " AND me.meal_date <= ?"
            params.append(end_date)

        query += " ORDER BY me.meal_date, me.meal_slot"

        cursor = conn.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def generate_meal_plan_shopping_list(
    plan_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    days_ahead: int | None = None,
    pantry_threshold: int = 30,
    combine_duplicates: bool = True,
    skip_items: list[str] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Generate shopping list for `user_id`'s meal plan(s).

    Can specify meals by:
    - plan_id: All meals in a specific plan
    - start_date + end_date: All meals in date range
    - days_ahead: Next N days from today

    Args:
        plan_id: Specific plan to shop for
        start_date: Start of date range
        end_date: End of date range
        days_ahead: Days from today to include
        pantry_threshold: Skip items above this pantry level
        combine_duplicates: Merge same ingredients
        skip_items: Ingredient names to skip
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Shopping list with items categorized by action
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    skip_items = skip_items or []

    # Determine date range
    if days_ahead is not None:
        start_date = _format_date(datetime.now())
        end_date = _format_date(datetime.now() + timedelta(days=days_ahead - 1))
    elif plan_id and not start_date:
        # Get dates from plan (owner-scoped)
        plan_result = get_meal_plan(plan_id, include_recipe_details=False, user_id=owner)
        if not plan_result.get("success"):
            return plan_result
        start_date = plan_result["plan"]["start_date"]
        end_date = plan_result["plan"]["end_date"]

    if not start_date or not end_date:
        return {"success": False, "error": "Must specify plan_id, date range, or days_ahead"}

    # Get meal entries (owner-scoped)
    entries = get_meal_entries_for_dates(plan_id, start_date, end_date, user_id=owner)

    if not entries:
        return {
            "success": True,
            "message": "No meals found for the specified date range",
            "date_range": {"start": start_date, "end": end_date},
            "ingredients": [],
            "recipes_included": [],
            "summary": {"items_to_add": 0, "items_to_skip": 0, "items_unknown": 0},
        }

    # Get pantry context (owner's pantry only)
    pantry_context: dict[str, dict[str, Any]] = {}
    try:
        pantry_items = get_pantry_status(apply_depletion=True, user_id=owner)
        for item in pantry_items:
            pantry_context[item["product_id"]] = {
                "level_percent": item.get("level_percent", 0),
                "status": item.get("status"),
                "days_until_empty": item.get("days_until_empty"),
                "description": item.get("description"),
            }
    except Exception:
        pass

    # Collect all ingredients from all recipes
    all_ingredients: dict[str, dict[str, Any]] = {}
    recipes_included = []
    recipe_info: dict[str, dict[str, Any]] = {}

    for entry in entries:
        recipe_id = entry["recipe_id"]
        recipe = get_recipe(recipe_id)

        if not recipe:
            continue

        servings_override = entry.get("servings_override")
        base_servings = recipe.get("servings", 4)
        scale = servings_override / base_servings if servings_override else 1.0

        if recipe_id not in recipe_info:
            recipe_info[recipe_id] = {
                "recipe_id": recipe_id,
                "recipe_name": recipe.get("name"),
                "times_used": 0,
            }
        recipe_info[recipe_id]["times_used"] += 1

        # Use recursive collector for sub-recipe/side support
        try:
            from ..tools.recipe_tools import _collect_ingredients_recursive

            collected = _collect_ingredients_recursive(recipe_id, scale)
            collected_ings = collected.get("ingredients", [])
        except Exception:
            # Fallback to direct ingredients if import fails
            collected_ings = [
                {
                    "name": ing.get("name", "Unknown"),
                    "product_id": ing.get("product_id"),
                    "scaled_quantity": _safe_float(ing.get("quantity"), 1) * scale,
                    "unit": ing.get("unit", ""),
                    "from_recipe_name": recipe.get("name"),
                }
                for ing in recipe.get("ingredients", [])
            ]

        for ing in collected_ings:
            ing_name = ing.get("name", "Unknown")
            product_id = ing.get("product_id")
            quantity = ing.get("scaled_quantity") or _safe_float(ing.get("quantity"), 1) * scale
            unit = ing.get("unit", "")

            # Key for combining
            key = product_id if product_id else ing_name.lower()

            if combine_duplicates and key in all_ingredients:
                existing = all_ingredients[key]
                if existing.get("unit") == unit:
                    existing["quantity"] += quantity
                existing["from_recipes"].append(ing.get("from_recipe_name") or recipe.get("name"))
            else:
                all_ingredients[key] = {
                    "name": ing_name,
                    "quantity": quantity,
                    "unit": unit,
                    "product_id": product_id,
                    "from_recipes": [ing.get("from_recipe_name") or recipe.get("name")],
                }

    recipes_included = list(recipe_info.values())

    # Categorize ingredients
    items_to_add = []
    items_to_skip = []
    items_unknown = []

    def _matches_skip(name: str) -> bool:
        name_lower = name.lower()
        for skip in skip_items:
            skip_lower = skip.lower()
            if skip_lower in name_lower or name_lower in skip_lower:
                return True
        return False

    for ing in all_ingredients.values():
        product_id = ing.get("product_id")
        name = ing.get("name", "Unknown")

        # Check user skip list
        user_skip = _matches_skip(name)

        # Check pantry
        pantry = pantry_context.get(product_id, {}) if product_id else {}
        pantry_level = pantry.get("level_percent")

        # Try fuzzy pantry match if no product_id
        if not product_id and not pantry_level:
            pantry_match = match_ingredient_to_pantry(name, None)
            if pantry_match:
                pantry_level = pantry_match.get("level_percent")
                pantry = {
                    "level_percent": pantry_level,
                    "description": pantry_match.get("description"),
                }

        in_pantry = pantry_level is not None

        # Determine action
        if user_skip:
            action = "SKIP"
            reason = "User specified to skip"
        elif in_pantry and pantry_level >= pantry_threshold:
            action = "SKIP"
            reason = f"Pantry: {pantry_level}% remaining"
        elif not product_id:
            action = "UNKNOWN"
            reason = "No product linked - search needed"
        else:
            action = "ADD"
            if in_pantry:
                reason = f"Pantry low: {pantry_level}%"
            else:
                reason = "Not in pantry"

        ingredient_info = {
            "name": name,
            "quantity": round(ing["quantity"], 2) if ing["quantity"] else 1,
            "unit": ing.get("unit", ""),
            "product_id": product_id,
            "from_recipes": list(set(ing["from_recipes"])),
            "action": action,
            "reason": reason,
            "pantry_level": pantry_level,
        }

        if action == "ADD":
            items_to_add.append(ingredient_info)
        elif action == "SKIP":
            items_to_skip.append(ingredient_info)
        else:
            items_unknown.append(ingredient_info)

    return {
        "success": True,
        "date_range": {
            "start": start_date,
            "end": end_date,
            "days_count": (_parse_date(end_date) - _parse_date(start_date)).days + 1,
        },
        "meals_included": len(entries),
        "recipes_included": recipes_included,
        "ingredients": items_to_add + items_to_skip + items_unknown,
        "items_to_add": items_to_add,
        "items_to_skip": items_to_skip,
        "items_unknown": items_unknown,
        "summary": {
            "items_to_add": len(items_to_add),
            "items_to_skip": len(items_to_skip),
            "items_unknown": len(items_unknown),
            "total_ingredients": len(all_ingredients),
        },
    }


# ============== Pantry Consumption ==============


def mark_meal_cooked(
    plan_id: str,
    meal_date: str,
    meal_slot: str,
    deduct_pantry: bool = True,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Mark a meal entry as cooked and optionally deduct ingredients from pantry.
    Only operates on rows owned by `user_id`.

    When deduct_pantry=True, each recipe ingredient is subtracted from the
    pantry using the actual quantity and unit specified in the recipe,
    scaled to the meal's servings.

    Args:
        plan_id: Plan identifier
        meal_date: Date YYYY-MM-DD
        meal_slot: 'breakfast', 'lunch', 'dinner', or 'snack'
        deduct_pantry: Whether to deduct ingredient quantities from pantry
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Dict with cooking confirmation and pantry deduction summary
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        # Get the meal entry (owner-scoped)
        cursor = conn.execute(
            """
            SELECT me.id, me.recipe_id, me.servings_override,
                   me.cooked_at, me.pantry_deducted
            FROM meal_entries me
            WHERE me.plan_id = ? AND me.meal_date = ? AND me.meal_slot = ?
              AND me.user_id = ?
        """,
            (plan_id, meal_date, meal_slot, owner),
        )
        entry = cursor.fetchone()

        if not entry:
            return {"success": False, "error": f"No meal found at {meal_slot} on {meal_date}"}

        entry_id = entry["id"]
        recipe_id = entry["recipe_id"]
        already_cooked = bool(entry["cooked_at"])
        already_deducted = bool(entry["pantry_deducted"])

        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

        recipe_name = recipe.get("name", recipe_id)
        base_servings = recipe.get("servings", 4)
        servings = entry["servings_override"] or base_servings
        scale = servings / base_servings if base_servings else 1.0

        now = datetime.now().isoformat()

        # Mark as cooked (owner-scoped guard on update)
        conn.execute(
            """
            UPDATE meal_entries
            SET cooked_at = ?
            WHERE id = ? AND user_id = ?
        """,
            (now, entry_id, owner),
        )
        conn.commit()

        deduction_summary = []
        deduction_errors = []
        skipped_no_quantity = []

        if deduct_pantry and not already_deducted:
            from ..analytics.pantry import consume_from_pantry

            # Collect ingredients (use recursive collector for sub-recipes)
            try:
                from ..tools.recipe_tools import _collect_ingredients_recursive

                collected = _collect_ingredients_recursive(recipe_id, scale)
                ingredients = collected.get("ingredients", [])
            except Exception:
                # Fallback to direct recipe ingredients
                ingredients = [
                    {
                        "name": ing.get("name", ""),
                        "scaled_quantity": (ing.get("quantity") or 1) * scale,
                        "unit": ing.get("unit", ""),
                        "product_id": ing.get("product_id"),
                        "from_recipe_name": recipe_name,
                    }
                    for ing in recipe.get("ingredients", [])
                ]

            for ing in ingredients:
                ing_name = ing.get("name", "")
                product_id = ing.get("product_id")
                qty = ing.get("scaled_quantity") or ing.get("quantity") or 0
                unit = ing.get("unit", "")

                if not product_id:
                    # Can't deduct without a product ID
                    skipped_no_quantity.append(ing_name)
                    continue

                if not qty or qty <= 0:
                    skipped_no_quantity.append(ing_name)
                    continue

                try:
                    result = consume_from_pantry(
                        product_id=product_id,
                        quantity=float(qty),
                        unit=unit or "each",
                        source_type="meal_plan",
                        source_id=str(entry_id),
                        source_description=(f"{recipe_name} — {meal_slot} on {meal_date}"),
                        user_id=owner,
                        recipe_id=recipe_id,
                        event_type="recipe_consumed",
                    )
                    if result.get("success"):
                        deduction_summary.append(
                            {
                                "ingredient": ing_name,
                                "product_id": product_id,
                                "consumed": qty,
                                "unit": unit,
                                "remaining_display": result.get("remaining_display"),
                                "new_level": result.get("new_level"),
                            }
                        )
                    else:
                        deduction_errors.append(
                            {
                                "ingredient": ing_name,
                                "error": result.get("error"),
                            }
                        )
                except Exception as e:
                    deduction_errors.append(
                        {
                            "ingredient": ing_name,
                            "error": str(e),
                        }
                    )

            if deduction_summary or not deduction_errors:
                # Mark pantry as deducted even if some items had no pantry entry
                conn.execute(
                    """
                    UPDATE meal_entries SET pantry_deducted = 1
                    WHERE id = ? AND user_id = ?
                """,
                    (entry_id, owner),
                )
                conn.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "meal_date": meal_date,
            "meal_slot": meal_slot,
            "recipe_id": recipe_id,
            "recipe_name": recipe_name,
            "servings": servings,
            "cooked_at": now,
            "was_already_cooked": already_cooked,
            "pantry_deducted": deduct_pantry and not already_deducted,
            "already_deducted": already_deducted,
            "deductions": deduction_summary,
            "deduction_errors": deduction_errors,
            "skipped_no_product_id": skipped_no_quantity,
            "summary": {
                "ingredients_deducted": len(deduction_summary),
                "errors": len(deduction_errors),
                "skipped": len(skipped_no_quantity),
            },
            "message": (
                f"Marked '{recipe_name}' as cooked. "
                f"Deducted {len(deduction_summary)} ingredient(s) from pantry."
                if deduct_pantry and not already_deducted
                else f"Marked '{recipe_name}' as cooked."
            ),
        }
    finally:
        conn.close()


def check_meal_pantry_availability(
    plan_id: str,
    meal_date: str,
    meal_slot: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Check if `user_id`'s pantry has enough for a specific planned meal.

    Returns per-ingredient availability with quantity comparisons
    so users know exactly what they're short on before cooking.

    Args:
        plan_id: Plan identifier
        meal_date: Date YYYY-MM-DD
        meal_slot: Meal slot name
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Dict with per-ingredient availability and a ready_to_cook flag
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT recipe_id, servings_override
            FROM meal_entries
            WHERE plan_id = ? AND meal_date = ? AND meal_slot = ?
              AND user_id = ?
        """,
            (plan_id, meal_date, meal_slot, owner),
        )
        entry = cursor.fetchone()

        if not entry:
            return {"success": False, "error": f"No meal at {meal_slot} on {meal_date}"}

        recipe_id = entry["recipe_id"]
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"success": False, "error": f"Recipe '{recipe_id}' not found"}

        base_servings = recipe.get("servings", 4)
        servings = entry["servings_override"] or base_servings
        scale = servings / base_servings if base_servings else 1.0

        # Collect ingredients
        try:
            from ..tools.recipe_tools import _collect_ingredients_recursive

            collected = _collect_ingredients_recursive(recipe_id, scale)
            ingredients = collected.get("ingredients", [])
        except Exception:
            ingredients = [
                {
                    "name": ing.get("name", ""),
                    "scaled_quantity": (ing.get("quantity") or 1) * scale,
                    "unit": ing.get("unit", ""),
                    "product_id": ing.get("product_id"),
                }
                for ing in recipe.get("ingredients", [])
            ]

        from ..analytics.pantry import check_pantry_quantity

        available = []
        not_enough = []
        unknown = []

        for ing in ingredients:
            ing_name = ing.get("name", "")
            product_id = ing.get("product_id")
            qty = ing.get("scaled_quantity") or ing.get("quantity") or 0
            unit = ing.get("unit", "each")

            check = check_pantry_quantity(product_id, ing_name, float(qty), unit)
            check["ingredient"] = ing_name
            check["needed_display"] = f"{qty} {unit}".strip()

            if not check["in_pantry"]:
                unknown.append(check)
            elif check["has_enough"]:
                available.append(check)
            else:
                not_enough.append(check)

        ready = len(not_enough) == 0 and len(unknown) == 0

        return {
            "success": True,
            "plan_id": plan_id,
            "meal_date": meal_date,
            "meal_slot": meal_slot,
            "recipe_id": recipe_id,
            "recipe_name": recipe.get("name"),
            "servings": servings,
            "ready_to_cook": ready,
            "available": available,
            "not_enough": not_enough,
            "unknown_pantry": unknown,
            "summary": {
                "total_ingredients": len(ingredients),
                "available_count": len(available),
                "insufficient_count": len(not_enough),
                "unknown_count": len(unknown),
            },
            "message": (
                "Ready to cook! All ingredients available."
                if ready
                else f"Missing or low on {len(not_enough)} ingredient(s). "
                f"{len(unknown)} not tracked in pantry."
            ),
        }
    finally:
        conn.close()


# ============== Utility Functions ==============


def get_week_view(start_date: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """
    Get a calendar-style view of `user_id`'s meals for a week.

    Args:
        start_date: Monday of the week (defaults to current week)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Week view with meals for each day
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    if start_date:
        try:
            week_start = _parse_date(start_date)
        except ValueError:
            return {"success": False, "error": "Invalid start_date format. Use YYYY-MM-DD"}
    else:
        # Get Monday of current week
        today = datetime.now()
        days_since_monday = today.weekday()
        week_start = today - timedelta(days=days_since_monday)

    week_end = week_start + timedelta(days=6)

    # Get all entries for this week (owner-scoped)
    entries = get_meal_entries_for_dates(
        start_date=_format_date(week_start),
        end_date=_format_date(week_end),
        user_id=owner,
    )

    # Organize by date
    entries_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        date = entry["meal_date"]
        slot = entry["meal_slot"]
        if date not in entries_by_date:
            entries_by_date[date] = {}

        recipe = get_recipe(entry["recipe_id"])
        entries_by_date[date][slot] = {
            "recipe_id": entry["recipe_id"],
            "recipe_name": recipe.get("name") if recipe else entry["recipe_id"],
            "servings": entry.get("servings_override")
            or (recipe.get("servings", 4) if recipe else 4),
        }

    # Build week view
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days = []

    for i in range(7):
        day_dt = week_start + timedelta(days=i)
        day_str = _format_date(day_dt)
        day_meals = entries_by_date.get(day_str, {})

        days.append(
            {
                "date": day_str,
                "day_name": day_names[i],
                "meals": {
                    "breakfast": day_meals.get("breakfast"),
                    "lunch": day_meals.get("lunch"),
                    "dinner": day_meals.get("dinner"),
                    "snack": day_meals.get("snack"),
                },
                "meal_count": len(day_meals),
            }
        )

    return {
        "success": True,
        "week_start": _format_date(week_start),
        "week_end": _format_date(week_end),
        "days": days,
        "total_meals": len(entries),
    }


def get_meal_plan_summary(plan_id: str, user_id: str | None = None) -> dict[str, Any]:
    """
    Get summary statistics for a meal plan owned by `user_id`.

    Args:
        plan_id: Plan identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Summary with meal counts, recipe stats, and pantry readiness
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    plan_result = get_meal_plan(plan_id, include_recipe_details=True, user_id=owner)
    if not plan_result.get("success"):
        return plan_result

    plan = plan_result["plan"]
    meals_by_date = plan_result.get("meals_by_date", {})

    # Count by slot
    slot_counts = {"breakfast": 0, "lunch": 0, "dinner": 0, "snack": 0}
    for date_meals in meals_by_date.values():
        for slot in date_meals:
            if slot in slot_counts:
                slot_counts[slot] += 1

    # Calculate coverage
    start_dt = _parse_date(plan["start_date"])
    end_dt = _parse_date(plan["end_date"])
    days_count = (end_dt - start_dt).days + 1
    max_meals = days_count * 4  # 4 slots per day
    coverage = plan_result["meal_count"] / max_meals if max_meals > 0 else 0

    # Check pantry readiness (owner-scoped)
    shopping = generate_meal_plan_shopping_list(plan_id=plan_id, user_id=owner)

    return {
        "success": True,
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "date_range": {"start": plan["start_date"], "end": plan["end_date"], "days": days_count},
        "meal_counts": {"total": plan_result["meal_count"], "by_slot": slot_counts},
        "recipes": {
            "unique_count": plan_result["unique_recipes"],
            "list": plan_result["recipe_summary"],
        },
        "coverage": round(coverage * 100, 1),
        "pantry_readiness": {
            "items_needed": shopping.get("summary", {}).get("items_to_add", 0),
            "items_available": shopping.get("summary", {}).get("items_to_skip", 0),
            "items_unknown": shopping.get("summary", {}).get("items_unknown", 0),
        },
    }


# ─── Persistent Meal Plan Views ───────────────────────────────────────────────

_SLOT_ORDER_SQL = (
    "CASE me.meal_slot "
    "WHEN 'breakfast' THEN 1 "
    "WHEN 'lunch' THEN 2 "
    "WHEN 'dinner' THEN 3 "
    "ELSE 4 END"
)


def _entry_to_meal_dict(row: Any, plan_name: str) -> dict[str, Any]:
    """Convert a meal_entries row + plan_name into a meal dict with recipe name."""
    recipe = _get_recipe_from_json(row["recipe_id"])
    return {
        "recipe_id": row["recipe_id"],
        "recipe_name": recipe.get("name") if recipe else row["recipe_id"],
        "plan_id": row["plan_id"],
        "plan_name": plan_name,
        "servings_override": row["servings_override"],
        "notes": row["notes"],
        "was_cooked": row["cooked_at"] is not None,
        "cooked_at": row["cooked_at"],
    }


def _load_plan_names(conn: Any, plan_ids: set, user_id: str) -> dict[str, str]:
    """Fetch plan names for a set of plan_ids owned by `user_id` in one query."""
    if not plan_ids:
        return {}
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"SELECT id, name FROM meal_plans WHERE id IN ({placeholders}) AND user_id = ?",
        [*plan_ids, user_id],
    ).fetchall()
    return {r["id"]: r["name"] for r in rows}


def get_today_meals(user_id: str | None = None) -> dict[str, Any]:
    """
    Return today's planned meals for `user_id`, grouped by slot.

    Args:
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, date, meals: {breakfast, lunch, dinner, snack}, meal_count}
        Each slot value is None (not planned) or a meal dict.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            f"""
            SELECT me.id, me.plan_id, me.meal_slot, me.meal_date,
                   me.cooked_at, me.notes, me.servings_override, me.recipe_id
            FROM meal_entries me
            WHERE me.meal_date = ? AND me.user_id = ?
            ORDER BY {_SLOT_ORDER_SQL}
            """,
            (today, owner),
        ).fetchall()

        plan_names = _load_plan_names(conn, {r["plan_id"] for r in rows}, owner)

        meals: dict[str, Any] = {
            "breakfast": None,
            "lunch": None,
            "dinner": None,
            "snack": None,
        }
        for row in rows:
            slot = row["meal_slot"]
            if slot in meals:
                meals[slot] = _entry_to_meal_dict(
                    row, plan_names.get(row["plan_id"], row["plan_id"])
                )

        return {
            "success": True,
            "date": today,
            "meals": meals,
            "meal_count": sum(1 for v in meals.values() if v is not None),
        }
    finally:
        conn.close()


def get_next_meal(user_id: str | None = None) -> dict[str, Any]:
    """
    Return the next upcoming (uncompleted) meal for `user_id` from now.

    Args:
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, meal: {...}} or {success, message} when nothing is planned.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute(
            f"""
            SELECT me.id, me.plan_id, me.meal_slot, me.meal_date,
                   me.cooked_at, me.notes, me.servings_override, me.recipe_id
            FROM meal_entries me
            WHERE me.meal_date >= ? AND me.user_id = ?
            ORDER BY me.meal_date ASC, {_SLOT_ORDER_SQL}
            LIMIT 1
            """,
            (today, owner),
        ).fetchone()

        if not row:
            return {
                "success": True,
                "meal": None,
                "message": "No upcoming meals planned.",
            }

        plan_names = _load_plan_names(conn, {row["plan_id"]}, owner)
        meal = _entry_to_meal_dict(row, plan_names.get(row["plan_id"], row["plan_id"]))
        meal["meal_date"] = row["meal_date"]
        meal["meal_slot"] = row["meal_slot"]

        return {
            "success": True,
            "meal": meal,
        }
    finally:
        conn.close()


def get_upcoming_meals(
    days: int = 7, from_date: str | None = None, user_id: str | None = None
) -> dict[str, Any]:
    """
    Return `user_id`'s planned meals for the next N days from from_date (default today).

    Args:
        days: Number of days to look ahead (1–90)
        from_date: Start date YYYY-MM-DD (default today)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, from_date, to_date, days, day_list: [...], total_meals}
        Each day_list entry: {date, day_of_week, meals: {slot: meal|None}, meal_count}
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    days = max(1, min(90, days))
    conn = get_db_connection()
    try:
        start = from_date or datetime.now().strftime("%Y-%m-%d")
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=days - 1)
        end = end_dt.strftime("%Y-%m-%d")

        rows = conn.execute(
            f"""
            SELECT me.meal_date, me.meal_slot, me.cooked_at,
                   me.notes, me.servings_override, me.recipe_id, me.plan_id
            FROM meal_entries me
            WHERE me.meal_date BETWEEN ? AND ? AND me.user_id = ?
            ORDER BY me.meal_date ASC, {_SLOT_ORDER_SQL}
            """,
            (start, end, owner),
        ).fetchall()

        plan_names = _load_plan_names(conn, {r["plan_id"] for r in rows}, owner)

        # Index by date → slot
        by_date: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = row["meal_date"]
            if d not in by_date:
                by_date[d] = {}
            by_date[d][row["meal_slot"]] = _entry_to_meal_dict(
                row, plan_names.get(row["plan_id"], row["plan_id"])
            )

        # Build day list (every day in range, even empty ones)
        day_list = []
        total_meals = 0
        for i in range(days):
            d_dt = start_dt + timedelta(days=i)
            d_str = d_dt.strftime("%Y-%m-%d")
            day_meals = by_date.get(d_str, {})
            slots = {
                "breakfast": day_meals.get("breakfast"),
                "lunch": day_meals.get("lunch"),
                "dinner": day_meals.get("dinner"),
                "snack": day_meals.get("snack"),
            }
            count = sum(1 for v in slots.values() if v is not None)
            total_meals += count
            day_list.append(
                {
                    "date": d_str,
                    "day_of_week": d_dt.strftime("%A"),
                    "meals": slots,
                    "meal_count": count,
                }
            )

        return {
            "success": True,
            "from_date": start,
            "to_date": end,
            "days": days,
            "day_list": day_list,
            "total_meals": total_meals,
        }
    finally:
        conn.close()


def get_meal_history(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Return past meal entries for `user_id`, grouped by date (most recent first).

    Args:
        days: Look-back window in days (used when start_date not given)
        start_date: Explicit start YYYY-MM-DD (overrides days)
        end_date: Explicit end YYYY-MM-DD (defaults to yesterday)
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, from_date, to_date, day_list: [...], total_meals, cooked_count}
        Each day_list entry: {date, day_of_week, meals: [...]}
        Each meal entry includes was_cooked, cooked_at, cooked_label.
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        if end_date is None:
            end_date = yesterday

        if start_date is None:
            cutoff_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days - 1)
            start_date = cutoff_dt.strftime("%Y-%m-%d")

        rows = conn.execute(
            f"""
            SELECT me.meal_date, me.meal_slot, me.cooked_at,
                   me.notes, me.servings_override, me.recipe_id, me.plan_id
            FROM meal_entries me
            WHERE me.meal_date < ?
              AND me.meal_date BETWEEN ? AND ?
              AND me.user_id = ?
            ORDER BY me.meal_date DESC, {_SLOT_ORDER_SQL}
            """,
            (today, start_date, end_date, owner),
        ).fetchall()

        plan_names = _load_plan_names(conn, {r["plan_id"] for r in rows}, owner)

        # Group by date
        by_date: dict[str, list] = {}
        for row in rows:
            d = row["meal_date"]
            if d not in by_date:
                by_date[d] = []
            entry = _entry_to_meal_dict(row, plan_names.get(row["plan_id"], row["plan_id"]))
            entry["meal_slot"] = row["meal_slot"]
            entry["cooked_label"] = (
                "✓ Cooked" if row["cooked_at"] else "— Planned (not marked cooked)"
            )
            by_date[d].append(entry)

        day_list = []
        for d_str in sorted(by_date.keys(), reverse=True):
            d_dt = datetime.strptime(d_str, "%Y-%m-%d")
            day_list.append(
                {
                    "date": d_str,
                    "day_of_week": d_dt.strftime("%A"),
                    "meals": by_date[d_str],
                }
            )

        total_meals = sum(len(v) for v in by_date.values())
        cooked_count = sum(1 for meals in by_date.values() for m in meals if m["was_cooked"])

        return {
            "success": True,
            "from_date": start_date,
            "to_date": end_date,
            "day_list": day_list,
            "total_meals": total_meals,
            "cooked_count": cooked_count,
            "planned_only_count": total_meals - cooked_count,
        }
    finally:
        conn.close()


def cleanup_expired_plans(retention_days: int = 90, user_id: str | None = None) -> dict[str, Any]:
    """
    Delete `user_id`'s meal plans whose end_date is more than retention_days ago.

    Cascade-deletes associated meal_entries (via FK).

    Args:
        retention_days: Plans are kept for this many days after end_date.
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, plans_removed, message}
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    retention_days = max(1, retention_days)
    conn = get_db_connection()
    try:
        # Identify plans to delete first (for informative response)
        expired = conn.execute(
            """
            SELECT id, name, end_date
            FROM meal_plans
            WHERE user_id = ?
              AND date(end_date, '+' || ? || ' days') < date('now')
            """,
            (owner, str(retention_days)),
        ).fetchall()

        if not expired:
            return {
                "success": True,
                "plans_removed": 0,
                "message": f"No expired plans found (retention: {retention_days} days).",
            }

        # Delete them (meal_entries cascade via FK)
        conn.execute(
            """
            DELETE FROM meal_plans
            WHERE user_id = ?
              AND date(end_date, '+' || ? || ' days') < date('now')
            """,
            (owner, str(retention_days)),
        )
        conn.commit()

        removed_names = [r["name"] for r in expired]
        return {
            "success": True,
            "plans_removed": len(expired),
            "removed_plan_names": removed_names,
            "message": (
                f"Removed {len(expired)} expired plan(s) "
                f"(older than {retention_days} days past end_date)."
            ),
        }
    finally:
        conn.close()


def get_plan_summary_stats(plan_id: str, user_id: str | None = None) -> dict[str, Any]:
    """
    Lightweight stats for the bottom stats bar, scoped to `user_id`'s rows.

    Args:
        plan_id: Plan identifier
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, plan_id, meal_count, unique_recipes, cooked_count}
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as total, COUNT(DISTINCT recipe_id) as unique_r, "
            "SUM(CASE WHEN cooked_at IS NOT NULL THEN 1 ELSE 0 END) as cooked "
            "FROM meal_entries WHERE plan_id = ? AND user_id = ?",
            (plan_id, owner),
        ).fetchone()
        if not row:
            return {
                "success": True,
                "plan_id": plan_id,
                "meal_count": 0,
                "unique_recipes": 0,
                "cooked_count": 0,
            }
        return {
            "success": True,
            "plan_id": plan_id,
            "meal_count": int(row[0] or 0),
            "unique_recipes": int(row[1] or 0),
            "cooked_count": int(row[2] or 0),
        }
    finally:
        conn.close()


def list_plans_for_api(
    include_templates: bool = False, limit: int = 50, user_id: str | None = None
) -> dict[str, Any]:
    """
    Clean plan listing for web API endpoints, scoped to `user_id`.

    Args:
        include_templates: If True return all plans; if False exclude templates.
        limit: Max rows to return.
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        {success, plans: [{id, name, start_date, end_date, plan_type, is_template}]}
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)
    conn = get_db_connection()
    try:
        if include_templates:
            rows = conn.execute(
                "SELECT id, name, start_date, end_date, plan_type, is_template "
                "FROM meal_plans WHERE user_id = ? "
                "ORDER BY start_date DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, start_date, end_date, plan_type, is_template "
                "FROM meal_plans WHERE user_id = ? AND is_template = 0 "
                "ORDER BY start_date DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        plans = [dict(r) for r in rows]
        for p in plans:
            p["is_template"] = bool(p.get("is_template"))
        return {"success": True, "plans": plans}
    finally:
        conn.close()
