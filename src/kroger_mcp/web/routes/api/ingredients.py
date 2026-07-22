"""Ingredients API endpoints — custom ingredient management."""

import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kroger_mcp.analytics.database import ensure_initialized, get_db_connection
from kroger_mcp.analytics.ingredients import get_active_ingredients, get_all_ingredients
from kroger_mcp.auth.dependencies import current_user_id

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CustomIngredientRequest(BaseModel):
    name: str
    severity: str = "warning"
    category: str | None = None
    reason: str | None = None
    aliases: list[str] | None = None


class UpdateIngredientRequest(BaseModel):
    severity: str | None = None
    category: str | None = None
    reason: str | None = None
    aliases: list[str] | None = None


# ---------------------------------------------------------------------------
# Custom ingredients endpoints
# ---------------------------------------------------------------------------


@router.get("/api/ingredients/custom")
async def list_custom(request: Request):
    """Get all active custom ingredients for the authenticated user."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        conn = get_db_connection()
        cursor = conn.execute(
            """
            SELECT * FROM custom_ingredients
            WHERE is_active = 1 AND user_id = ?
            ORDER BY ingredient_name
            """,
            (user_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        # Normalize ingredient_name -> name for UI consistency
        result = [
            {
                "name": r["ingredient_name"],
                "severity": r["severity"],
                "category": r.get("category") or "",
                "reason": r.get("reason") or "",
                "aliases": json.loads(r["aliases"]) if r.get("aliases") else [],
                "created_at": r.get("created_at", ""),
            }
            for r in rows
        ]
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to get custom ingredients: {str(e)}"},
        )


@router.post("/api/ingredients/custom")
async def add_custom(request: Request, body: CustomIngredientRequest):
    """Add a new custom ingredient for the authenticated user."""
    try:
        ensure_initialized()
        if body.severity not in ("critical", "warning", "watch"):
            return JSONResponse(
                status_code=400,
                content={"error": "severity must be critical, warning, or watch"},
            )
        user_id = current_user_id(request)
        conn = get_db_connection()
        now = datetime.now().isoformat()
        aliases_json = json.dumps(body.aliases or [])
        conn.execute(
            """
            INSERT INTO custom_ingredients
                (user_id, ingredient_name, severity, category, reason, aliases,
                 source, created_at, modified_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, 1)
            ON CONFLICT(user_id, ingredient_name) DO UPDATE SET
                severity = excluded.severity,
                category = excluded.category,
                reason = excluded.reason,
                aliases = excluded.aliases,
                modified_at = excluded.modified_at,
                is_active = 1
            """,
            (
                user_id,
                body.name,
                body.severity,
                body.category,
                body.reason,
                aliases_json,
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()

        # Invalidate pattern cache so next safety check picks up the new ingredient.
        try:
            from kroger_mcp.analytics.ingredients import get_compiled_patterns

            get_compiled_patterns(user_id=user_id, force_refresh=True)
        except Exception:
            pass

        return JSONResponse(content={"success": True, "name": body.name})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to add ingredient: {str(e)}"},
        )


@router.put("/api/ingredients/custom/{name}")
async def update_custom(request: Request, name: str, body: UpdateIngredientRequest):
    """Update an existing custom ingredient for the authenticated user."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        conn = get_db_connection()
        now = datetime.now().isoformat()

        # Build SET clause dynamically for only provided fields
        updates = []
        params = []

        if body.severity is not None:
            if body.severity not in ("critical", "warning", "watch"):
                conn.close()
                return JSONResponse(
                    status_code=400,
                    content={"error": "severity must be critical, warning, or watch"},
                )
            updates.append("severity = ?")
            params.append(body.severity)
        if body.category is not None:
            updates.append("category = ?")
            params.append(body.category)
        if body.reason is not None:
            updates.append("reason = ?")
            params.append(body.reason)
        if body.aliases is not None:
            updates.append("aliases = ?")
            params.append(json.dumps(body.aliases))

        if not updates:
            conn.close()
            return JSONResponse(content={"success": True, "message": "Nothing to update"})

        updates.append("modified_at = ?")
        params.append(now)
        params.extend([user_id, name])

        conn.execute(
            f"UPDATE custom_ingredients SET {', '.join(updates)} "
            "WHERE user_id = ? AND ingredient_name = ? COLLATE NOCASE",
            params,
        )
        conn.commit()
        conn.close()

        try:
            from kroger_mcp.analytics.ingredients import get_compiled_patterns

            get_compiled_patterns(user_id=user_id, force_refresh=True)
        except Exception:
            pass

        return JSONResponse(content={"success": True, "name": name})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to update ingredient: {str(e)}"},
        )


@router.delete("/api/ingredients/custom/{name}")
async def remove_custom(request: Request, name: str):
    """Soft-delete a custom ingredient for the authenticated user."""
    try:
        ensure_initialized()
        user_id = current_user_id(request)
        conn = get_db_connection()
        conn.execute(
            """
            UPDATE custom_ingredients
            SET is_active = 0, modified_at = ?
            WHERE user_id = ? AND ingredient_name = ? COLLATE NOCASE
            """,
            (datetime.now().isoformat(), user_id, name),
        )
        deleted = conn.total_changes
        conn.commit()
        conn.close()

        try:
            from kroger_mcp.analytics.ingredients import get_compiled_patterns

            get_compiled_patterns(user_id=user_id, force_refresh=True)
        except Exception:
            pass

        if deleted:
            return JSONResponse(content={"success": True, "name": name})
        return JSONResponse(
            status_code=404,
            content={"error": f"Ingredient '{name}' not found"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to remove ingredient: {str(e)}"},
        )


# ---------------------------------------------------------------------------
# All ingredients endpoint
# ---------------------------------------------------------------------------


@router.get("/api/ingredients/all")
async def list_all(request: Request):
    """Get all ingredients — system defaults merged with the caller's custom overrides."""
    try:
        ensure_initialized()
        ingredients = get_active_ingredients(user_id=current_user_id(request), include_custom=True)
        result = [
            {
                "name": ing.get("name", ""),
                "severity": ing.get("severity", "watch"),
                "category": ing.get("category", ""),
                "reason": ing.get("reason", ""),
                "is_custom": ing.get("source") == "custom",
            }
            for ing in ingredients
        ]
        return JSONResponse(content=result)
    except Exception as e:
        # Fallback to just the hardcoded list
        try:
            result = [
                {
                    "name": ing["name"],
                    "severity": ing["severity"],
                    "category": ing["category"],
                    "reason": ing["reason"],
                    "is_custom": False,
                }
                for ing in get_all_ingredients()
            ]
            return JSONResponse(content=result)
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"error": f"Failed to get ingredients: {str(e)}"},
            )
