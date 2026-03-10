"""API routes for meal plan write operations."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


class CreatePlanBody(BaseModel):
    name: str
    start_date: str
    plan_type: str = "weekly"
    description: Optional[str] = None


class AddMealBody(BaseModel):
    recipe_id: str
    meal_date: str
    meal_slot: str = "dinner"


@router.post("/api/meal-plan")
async def create_meal_plan(body: CreatePlanBody):
    """Create a new meal plan."""
    try:
        from kroger_mcp.analytics.meal_planning import create_meal_plan as _create
        result = _create(
            name=body.name,
            start_date=body.start_date,
            plan_type=body.plan_type,
            description=body.description,
        )
        if isinstance(result, dict) and not result.get("success", True):
            return JSONResponse(status_code=400, content=result)
        return result
    except ImportError:
        pass
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    # Fallback: direct SQL insert
    try:
        from kroger_mcp.analytics.database import get_db_cursor, ensure_initialized
        ensure_initialized()
        plan_id = str(uuid.uuid4())
        now = datetime.now().isoformat()

        # Compute end_date
        from datetime import timedelta
        try:
            start_dt = datetime.strptime(body.start_date, "%Y-%m-%d")
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid start_date format. Use YYYY-MM-DD"},
            )
        days = 6 if body.plan_type != "monthly" else 29
        end_date = (start_dt + timedelta(days=days)).strftime("%Y-%m-%d")

        with get_db_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO meal_plans
                    (id, name, description, plan_type, start_date, end_date,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    body.name,
                    body.description,
                    body.plan_type,
                    body.start_date,
                    end_date,
                    now,
                    now,
                ),
            )
        return {
            "success": True,
            "plan_id": plan_id,
            "name": body.name,
            "start_date": body.start_date,
            "end_date": end_date,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.delete("/api/meal-plan/{plan_id}")
async def delete_meal_plan(plan_id: str):
    """Delete a meal plan and its entries."""
    try:
        from kroger_mcp.analytics.database import get_db_cursor, ensure_initialized
        ensure_initialized()
        with get_db_cursor() as cursor:
            cursor.execute(
                "DELETE FROM meal_entries WHERE plan_id = ?", (plan_id,)
            )
            cursor.execute(
                "DELETE FROM meal_plans WHERE id = ?", (plan_id,)
            )
            if cursor.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Plan '{plan_id}' not found"},
                )
        return {"success": True, "plan_id": plan_id}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.post("/api/meal-plan/{plan_id}/meals")
async def add_meal_to_plan(plan_id: str, body: AddMealBody):
    """Assign a recipe to a meal slot in a plan."""
    try:
        from kroger_mcp.analytics.database import get_db_cursor, ensure_initialized
        ensure_initialized()
        now = datetime.now().isoformat()
        with get_db_cursor() as cursor:
            # Delete any existing entry for this slot first, then insert fresh
            cursor.execute(
                "DELETE FROM meal_entries WHERE plan_id = ? AND meal_date = ? AND meal_slot = ?",
                (plan_id, body.meal_date, body.meal_slot),
            )
            cursor.execute(
                """
                INSERT INTO meal_entries
                    (plan_id, recipe_id, meal_date, meal_slot, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (plan_id, body.recipe_id, body.meal_date, body.meal_slot, now),
            )
            entry_id = cursor.lastrowid
        return {
            "success": True,
            "entry_id": entry_id,
            "plan_id": plan_id,
            "recipe_id": body.recipe_id,
            "meal_date": body.meal_date,
            "meal_slot": body.meal_slot,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
