"""
Ingredient management tools for dynamic ingredient filter customization.
"""

import asyncio
import json
from typing import Any, Literal

from fastmcp import Context
from pydantic import Field

from ..analytics.database import get_db_connection, insert_returning_id
from ..analytics.ingredients import get_compiled_patterns
from ..auth.dependencies import mcp_user_id


def register_tools(mcp):
    """Register ingredient management tools with the FastMCP server."""

    @mcp.tool()
    async def ingredients(
        action: Literal[
            "add_custom",
            "edit_custom",
            "remove_custom",
            "list_custom",
            "override_system",
            "reset_to_default",
            "get_info",
            "import_list",
            "export_list",
            "preview_impact",
        ] = Field(
            description=(
                "add_custom — add ingredients (batch: batch_ingredients, max 20). "
                "override_system — change built-in ingredient severity. "
                "preview_impact — see effect before committing. "
                "Other: edit_custom|remove_custom|list_custom|reset_to_default|get_info|import_list|export_list"
            )
        ),
        ingredient_name: str | None = Field(
            default=None,
            description="Ingredient name",
        ),
        severity: Literal["critical", "warning", "watch"] | None = Field(
            default=None,
            description="critical|warning|watch",
        ),
        category: str | None = Field(
            default=None,
            description="Category e.g. preservative",
        ),
        reason: str | None = Field(
            default=None,
            description="Why to avoid this ingredient",
        ),
        aliases: list[str] | None = Field(
            default=None,
            description="Alternative names/spellings",
        ),
        notes: str | None = Field(
            default=None,
            description="Personal notes",
        ),
        batch_ingredients: list[dict[str, Any]] | None = Field(
            default=None,
            description="Batch: [{ingredient_name, severity, category, reason, aliases, notes}] max 20",
        ),
        new_severity: Literal["critical", "warning", "watch"] | None = Field(
            default=None,
            description="New severity",
        ),
        new_reason: str | None = Field(
            default=None,
            description="New reason",
        ),
        add_aliases: list[str] | None = Field(
            default=None,
            description="Additional aliases to add",
        ),
        new_notes: str | None = Field(
            default=None,
            description="New notes",
        ),
        permanent: bool | None = Field(
            default=False,
            description="Permanently delete vs soft-delete",
        ),
        include_inactive: bool | None = Field(
            default=False,
            description="Include deactivated ingredients",
        ),
        filter_severity: Literal["critical", "warning", "watch"] | None = Field(
            default=None,
            description="Filter by severity",
        ),
        filter_category: str | None = Field(
            default=None,
            description="Filter by category",
        ),
        hide: bool | None = Field(
            default=False,
            description="Hide from active filter",
        ),
        import_data: str | None = Field(
            default=None,
            description="JSON string of ingredients to import",
        ),
        merge_strategy: Literal["replace", "merge", "skip_existing"] | None = Field(
            default="merge",
            description="replace|merge|skip_existing",
        ),
        include_system_overrides: bool | None = Field(
            default=True,
            description="Include system overrides in export",
        ),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Customize the ingredient safety filter.

        Two layers: system ingredients (62 built-in) and custom ingredients (yours).
        override_system — change severity/disable a system ingredient.
        add_custom — add your own (batch: batch_ingredients, max 20).
        preview_impact — see how many purchased products would be affected.
        Changes take effect immediately (no restart needed).

        Other: edit_custom, remove_custom, list_custom, reset_to_default, get_info,
        import_list, export_list
        """
        return await asyncio.to_thread(
            _ingredients_impl,
            action,
            ingredient_name,
            severity,
            category,
            reason,
            aliases,
            notes,
            batch_ingredients,
            new_severity,
            new_reason,
            add_aliases,
            new_notes,
            permanent,
            include_inactive,
            filter_severity,
            filter_category,
            hide,
            import_data,
            merge_strategy,
            include_system_overrides,
            ctx,
        )

    def _ingredients_impl(
        action,
        ingredient_name,
        severity,
        category,
        reason,
        aliases,
        notes,
        batch_ingredients,
        new_severity,
        new_reason,
        add_aliases,
        new_notes,
        permanent,
        include_inactive,
        filter_severity,
        filter_category,
        hide,
        import_data,
        merge_strategy,
        include_system_overrides,
        ctx,
    ):
        # Resolve the MCP invocation's user once; all SQL is scoped to it.
        user_id = mcp_user_id()

        match action:
            case "add_custom":
                if batch_ingredients is not None:
                    if len(batch_ingredients) > 20:
                        return {
                            "success": False,
                            "error": "Maximum 20 ingredients per batch request",
                        }

                    for item in batch_ingredients:
                        if "ingredient_name" not in item or "severity" not in item:
                            return {
                                "success": False,
                                "error": "Each ingredient must have 'ingredient_name' and 'severity' fields",
                            }
                        if item["severity"] not in ["critical", "warning", "watch"]:
                            return {
                                "success": False,
                                "error": f"Invalid severity '{item['severity']}' for {item['ingredient_name']}. Must be 'critical', 'warning', or 'watch'",
                            }

                    ing_list = batch_ingredients
                    is_batch = True
                else:
                    if not ingredient_name or not severity:
                        return {
                            "success": False,
                            "error": "Single mode requires both ingredient_name and severity",
                        }
                    ing_list = [
                        {
                            "ingredient_name": ingredient_name,
                            "severity": severity,
                            "category": category,
                            "reason": reason,
                            "aliases": aliases,
                            "notes": notes,
                        }
                    ]
                    is_batch = False

                if ctx and is_batch:
                    ctx.info(f"Adding {len(ing_list)} custom ingredients")

                conn = get_db_connection()
                results = {}

                try:
                    for item in ing_list:
                        name = item["ingredient_name"]
                        sev = item["severity"]
                        cat = item.get("category")
                        rsn = item.get("reason")
                        als = item.get("aliases")
                        nts = item.get("notes")

                        try:
                            cursor = conn.execute(
                                "SELECT id FROM custom_ingredients "
                                "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                (user_id, name),
                            )
                            if cursor.fetchone():
                                results[name] = {
                                    "success": False,
                                    "error": f"Ingredient '{name}' already exists. Use ingredients(action='edit_custom') to modify it.",
                                }
                                continue

                            aliases_json = json.dumps(als) if als else None
                            new_id = insert_returning_id(
                                conn,
                                """
                                INSERT INTO custom_ingredients
                                    (user_id, ingredient_name, severity, category, reason, aliases, notes)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (user_id, name, sev, cat, rsn, aliases_json, nts),
                            )
                            conn.commit()

                            results[name] = {
                                "success": True,
                                "message": f"Added custom ingredient: {name}",
                                "ingredient_id": new_id,
                                "details": {
                                    "name": name,
                                    "severity": sev,
                                    "category": cat,
                                    "reason": rsn,
                                    "aliases": als or [],
                                },
                            }

                        except Exception as e:
                            results[name] = {
                                "success": False,
                                "error": f"Failed to add {name}: {str(e)}",
                            }

                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    if is_batch:
                        success_count = sum(1 for r in results.values() if r.get("success"))
                        return {
                            "success": True,
                            "results": results,
                            "summary": {
                                "total": len(ing_list),
                                "successful": success_count,
                                "failed": len(ing_list) - success_count,
                            },
                        }
                    else:
                        return results[ing_list[0]["ingredient_name"]]

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Failed to add ingredients: {str(e)}",
                    }
                finally:
                    conn.close()

            case "edit_custom":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if ctx:
                    ctx.info(f"Editing custom ingredient: {ingredient_name}")

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        "SELECT * FROM custom_ingredients "
                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                        (user_id, ingredient_name),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {
                            "success": False,
                            "error": f"Custom ingredient '{ingredient_name}' not found. Use ingredients(action='add_custom') to create it.",
                        }

                    updates = []
                    params = []

                    if new_severity:
                        updates.append("severity = ?")
                        params.append(new_severity)

                    if new_reason:
                        updates.append("reason = ?")
                        params.append(new_reason)

                    if new_notes:
                        updates.append("notes = ?")
                        params.append(new_notes)

                    if add_aliases:
                        current_aliases = json.loads(row["aliases"]) if row["aliases"] else []
                        merged_aliases = list(set(current_aliases + add_aliases))
                        updates.append("aliases = ?")
                        params.append(json.dumps(merged_aliases))

                    if not updates:
                        return {"success": False, "error": "No changes specified"}

                    updates.append("modified_at = CURRENT_TIMESTAMP")
                    params.extend([user_id, ingredient_name])

                    conn.execute(
                        f"UPDATE custom_ingredients SET {', '.join(updates)} "
                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                        params,
                    )
                    conn.commit()

                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    return {
                        "success": True,
                        "message": f"Updated custom ingredient: {ingredient_name}",
                        "changes": {
                            "severity": new_severity,
                            "reason": new_reason,
                            "added_aliases": add_aliases,
                            "notes": new_notes,
                        },
                    }

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Failed to edit ingredient: {str(e)}",
                    }
                finally:
                    conn.close()

            case "remove_custom":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if ctx:
                    ctx.info(f"Removing custom ingredient: {ingredient_name}")

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        "SELECT id FROM custom_ingredients "
                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                        (user_id, ingredient_name),
                    )
                    if not cursor.fetchone():
                        return {
                            "success": False,
                            "error": f"Custom ingredient '{ingredient_name}' not found",
                        }

                    if permanent:
                        conn.execute(
                            "DELETE FROM custom_ingredients "
                            "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                            (user_id, ingredient_name),
                        )
                        message = f"Permanently deleted custom ingredient: {ingredient_name}"
                    else:
                        conn.execute(
                            "UPDATE custom_ingredients "
                            "SET is_active = 0, modified_at = CURRENT_TIMESTAMP "
                            "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                            (user_id, ingredient_name),
                        )
                        message = f"Deactivated custom ingredient: {ingredient_name} (can be restored later)"

                    conn.commit()
                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    return {
                        "success": True,
                        "message": message,
                        "permanent": permanent or False,
                    }

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Failed to remove ingredient: {str(e)}",
                    }
                finally:
                    conn.close()

            case "list_custom":
                if ctx:
                    ctx.info("Listing custom ingredients")

                conn = get_db_connection()
                try:
                    query = "SELECT * FROM custom_ingredients WHERE user_id = ?"
                    params: list[Any] = [user_id]

                    if not (include_inactive or False):
                        query += " AND is_active = 1"

                    if filter_severity:
                        query += " AND severity = ?"
                        params.append(filter_severity)

                    if filter_category:
                        query += " AND category = ?"
                        params.append(filter_category)

                    query += " ORDER BY severity, ingredient_name"

                    cursor = conn.execute(query, params)
                    rows = cursor.fetchall()

                    ing_list = []
                    for row in rows:
                        ing_list.append(
                            {
                                "name": row["ingredient_name"],
                                "severity": row["severity"],
                                "category": row["category"],
                                "reason": row["reason"],
                                "aliases": json.loads(row["aliases"]) if row["aliases"] else [],
                                "source": row["source"],
                                "is_active": bool(row["is_active"]),
                                "created_at": row["created_at"],
                                "modified_at": row["modified_at"],
                                "notes": row["notes"],
                            }
                        )

                    by_severity = {
                        "critical": [i for i in ing_list if i["severity"] == "critical"],
                        "warning": [i for i in ing_list if i["severity"] == "warning"],
                        "watch": [i for i in ing_list if i["severity"] == "watch"],
                    }

                    return {
                        "success": True,
                        "total_count": len(ing_list),
                        "active_count": sum(1 for i in ing_list if i["is_active"]),
                        "by_severity": {
                            "critical": len(by_severity["critical"]),
                            "warning": len(by_severity["warning"]),
                            "watch": len(by_severity["watch"]),
                        },
                        "ingredients": ing_list,
                    }

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to list ingredients: {str(e)}",
                    }
                finally:
                    conn.close()

            case "override_system":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if ctx:
                    ctx.info(f"Overriding system ingredient: {ingredient_name}")

                from ..analytics.ingredients import BAD_INGREDIENTS

                system_names = {ing.name.lower() for ing in BAD_INGREDIENTS}
                if ingredient_name.lower() not in system_names:
                    return {
                        "success": False,
                        "error": f"'{ingredient_name}' is not a system ingredient. Use ingredients(action='add_custom') for custom ingredients.",
                    }

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        "SELECT id FROM ingredient_overrides "
                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                        (user_id, ingredient_name),
                    )
                    existing = cursor.fetchone()

                    aliases_json = json.dumps(add_aliases) if add_aliases else None

                    if existing:
                        updates = []
                        params = []

                        if new_severity:
                            updates.append("override_severity = ?")
                            params.append(new_severity)

                        if new_reason:
                            updates.append("override_reason = ?")
                            params.append(new_reason)

                        if add_aliases:
                            cursor = conn.execute(
                                "SELECT additional_aliases FROM ingredient_overrides WHERE id = ?",
                                (existing["id"],),
                            )
                            current = cursor.fetchone()["additional_aliases"]
                            current_list = json.loads(current) if current else []
                            merged = list(set(current_list + add_aliases))
                            updates.append("additional_aliases = ?")
                            params.append(json.dumps(merged))

                        if hide:
                            updates.append("is_hidden = 1")

                        if notes:
                            updates.append("notes = ?")
                            params.append(notes)

                        updates.append("modified_at = CURRENT_TIMESTAMP")
                        params.extend([user_id, ingredient_name])

                        if updates:
                            conn.execute(
                                f"UPDATE ingredient_overrides SET {', '.join(updates)} "
                                "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                params,
                            )
                    else:
                        conn.execute(
                            """
                            INSERT INTO ingredient_overrides
                                (user_id, ingredient_name, override_severity, override_reason,
                                 additional_aliases, is_hidden, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                user_id,
                                ingredient_name,
                                new_severity,
                                new_reason,
                                aliases_json,
                                1 if hide else 0,
                                notes,
                            ),
                        )

                    conn.commit()
                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    return {
                        "success": True,
                        "message": f"Overrode system ingredient: {ingredient_name}",
                        "changes": {
                            "severity": new_severity,
                            "reason": new_reason,
                            "aliases": add_aliases,
                            "hidden": hide,
                            "notes": notes,
                        },
                    }

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Failed to override ingredient: {str(e)}",
                    }
                finally:
                    conn.close()

            case "reset_to_default":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if ctx:
                    ctx.info(f"Resetting ingredient to default: {ingredient_name}")

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        "DELETE FROM ingredient_overrides "
                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                        (user_id, ingredient_name),
                    )

                    if cursor.rowcount == 0:
                        return {
                            "success": False,
                            "error": f"No override found for '{ingredient_name}'",
                        }

                    conn.commit()
                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    return {
                        "success": True,
                        "message": f"Reset ingredient to default: {ingredient_name}",
                    }

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Failed to reset ingredient: {str(e)}",
                    }
                finally:
                    conn.close()

            case "get_info":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if ctx:
                    ctx.info(f"Getting info for ingredient: {ingredient_name}")

                from ..analytics.ingredients import BAD_INGREDIENTS

                system_ing = None
                for ing in BAD_INGREDIENTS:
                    if ing.name.lower() == ingredient_name.lower():
                        system_ing = ing
                        break

                conn = get_db_connection()
                try:
                    result = {}

                    if system_ing:
                        result["source"] = "system"
                        result["default_settings"] = {
                            "name": system_ing.name,
                            "severity": system_ing.severity.value,
                            "category": system_ing.category,
                            "reason": system_ing.reason,
                            "aliases": list(system_ing.aliases),
                            "key": system_ing.key,
                        }

                        cursor = conn.execute(
                            "SELECT * FROM ingredient_overrides "
                            "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                            (user_id, ingredient_name),
                        )
                        override = cursor.fetchone()

                        if override:
                            result["has_override"] = True
                            result["current_settings"] = {
                                "name": system_ing.name,
                                "severity": override["override_severity"]
                                or system_ing.severity.value,
                                "category": system_ing.category,
                                "reason": override["override_reason"] or system_ing.reason,
                                "aliases": list(system_ing.aliases)
                                + (
                                    json.loads(override["additional_aliases"])
                                    if override["additional_aliases"]
                                    else []
                                ),
                                "is_hidden": bool(override["is_hidden"]),
                                "notes": override["notes"],
                            }
                        else:
                            result["has_override"] = False
                            result["current_settings"] = result["default_settings"]

                    else:
                        cursor = conn.execute(
                            "SELECT * FROM custom_ingredients "
                            "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                            (user_id, ingredient_name),
                        )
                        custom = cursor.fetchone()

                        if custom:
                            result["source"] = "custom"
                            result["current_settings"] = {
                                "name": custom["ingredient_name"],
                                "severity": custom["severity"],
                                "category": custom["category"],
                                "reason": custom["reason"],
                                "aliases": (
                                    json.loads(custom["aliases"]) if custom["aliases"] else []
                                ),
                                "is_active": bool(custom["is_active"]),
                                "created_at": custom["created_at"],
                                "modified_at": custom["modified_at"],
                                "notes": custom["notes"],
                            }
                        else:
                            return {
                                "success": False,
                                "error": f"Ingredient '{ingredient_name}' not found",
                            }

                    result["success"] = True
                    return result

                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Failed to get ingredient info: {str(e)}",
                    }
                finally:
                    conn.close()

            case "import_list":
                if not import_data:
                    return {"success": False, "error": "import_data (JSON string) is required"}
                if ctx:
                    ctx.info("Importing ingredient list")

                try:
                    data = json.loads(import_data)
                except json.JSONDecodeError as e:
                    return {"success": False, "error": f"Invalid JSON: {str(e)}"}

                conn = get_db_connection()
                imported_count = 0
                skipped_count = 0
                errors = []
                strategy = merge_strategy or "merge"

                try:
                    if "ingredients" in data:
                        for ing in data["ingredients"]:
                            try:
                                name = ing["name"]
                                sev = ing["severity"]

                                cursor = conn.execute(
                                    "SELECT id FROM custom_ingredients "
                                    "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                    (user_id, name),
                                )
                                exists = cursor.fetchone()

                                if exists:
                                    if strategy == "skip_existing":
                                        skipped_count += 1
                                        continue
                                    elif strategy == "replace":
                                        conn.execute(
                                            "DELETE FROM custom_ingredients "
                                            "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                            (user_id, name),
                                        )

                                if not exists or strategy == "replace":
                                    aliases_json = json.dumps(ing.get("aliases", []))
                                    conn.execute(
                                        """
                                        INSERT INTO custom_ingredients
                                            (user_id, ingredient_name, severity, category,
                                             reason, aliases, source)
                                        VALUES (?, ?, ?, ?, ?, ?, 'imported')
                                        """,
                                        (
                                            user_id,
                                            name,
                                            sev,
                                            ing.get("category"),
                                            ing.get("reason"),
                                            aliases_json,
                                        ),
                                    )
                                    imported_count += 1

                            except Exception as e:
                                errors.append(
                                    f"Failed to import '{ing.get('name', 'unknown')}': {str(e)}"
                                )

                    if "overrides" in data:
                        for override in data["overrides"]:
                            try:
                                name = override["name"]

                                cursor = conn.execute(
                                    "SELECT id FROM ingredient_overrides "
                                    "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                    (user_id, name),
                                )
                                exists = cursor.fetchone()

                                if exists and strategy == "skip_existing":
                                    skipped_count += 1
                                    continue

                                if exists and strategy == "replace":
                                    conn.execute(
                                        "DELETE FROM ingredient_overrides "
                                        "WHERE user_id = ? AND LOWER(ingredient_name) = LOWER(?)",
                                        (user_id, name),
                                    )

                                if not exists or strategy == "replace":
                                    aliases_json = (
                                        json.dumps(override.get("add_aliases", []))
                                        if override.get("add_aliases")
                                        else None
                                    )
                                    conn.execute(
                                        """
                                        INSERT INTO ingredient_overrides
                                            (user_id, ingredient_name, override_severity,
                                             override_reason, additional_aliases, is_hidden)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                        """,
                                        (
                                            user_id,
                                            name,
                                            override.get("new_severity"),
                                            override.get("new_reason"),
                                            aliases_json,
                                            override.get("hide", 0),
                                        ),
                                    )
                                    imported_count += 1

                            except Exception as e:
                                errors.append(
                                    f"Failed to import override for '{override.get('name', 'unknown')}': {str(e)}"
                                )

                    conn.commit()
                    get_compiled_patterns(user_id=user_id, force_refresh=True)

                    return {
                        "success": True,
                        "message": f"Imported {imported_count} items, skipped {skipped_count}",
                        "imported_count": imported_count,
                        "skipped_count": skipped_count,
                        "errors": errors if errors else None,
                    }

                except Exception as e:
                    conn.rollback()
                    return {
                        "success": False,
                        "error": f"Import failed: {str(e)}",
                        "errors": errors,
                    }
                finally:
                    conn.close()

            case "export_list":
                if ctx:
                    ctx.info("Exporting ingredient list")

                conn = get_db_connection()
                try:
                    cursor = conn.execute(
                        "SELECT * FROM custom_ingredients "
                        "WHERE is_active = 1 AND user_id = ? "
                        "ORDER BY severity, ingredient_name",
                        (user_id,),
                    )
                    custom_rows = cursor.fetchall()

                    ing_list = []
                    for row in custom_rows:
                        ing_list.append(
                            {
                                "name": row["ingredient_name"],
                                "severity": row["severity"],
                                "category": row["category"],
                                "reason": row["reason"],
                                "aliases": json.loads(row["aliases"]) if row["aliases"] else [],
                            }
                        )

                    overrides = []
                    if include_system_overrides if include_system_overrides is not None else True:
                        cursor = conn.execute(
                            "SELECT * FROM ingredient_overrides "
                            "WHERE user_id = ? ORDER BY ingredient_name",
                            (user_id,),
                        )
                        override_rows = cursor.fetchall()

                        for row in override_rows:
                            override_data = {"name": row["ingredient_name"]}
                            if row["override_severity"]:
                                override_data["new_severity"] = row["override_severity"]
                            if row["override_reason"]:
                                override_data["new_reason"] = row["override_reason"]
                            if row["additional_aliases"]:
                                override_data["add_aliases"] = json.loads(row["additional_aliases"])
                            if row["is_hidden"]:
                                override_data["hide"] = True
                            overrides.append(override_data)

                    import datetime as dt

                    export_data = {
                        "ingredients": ing_list,
                        "overrides": overrides,
                        "export_date": dt.datetime.now().isoformat(),
                        "version": "1.0",
                    }

                    return {
                        "success": True,
                        "ingredient_count": len(ing_list),
                        "override_count": len(overrides),
                        "export_data": json.dumps(export_data, indent=2),
                    }

                except Exception as e:
                    return {"success": False, "error": f"Export failed: {str(e)}"}
                finally:
                    conn.close()

            case "preview_impact":
                if not ingredient_name:
                    return {"success": False, "error": "ingredient_name is required"}
                if not severity:
                    return {"success": False, "error": "severity is required"}
                if ctx:
                    ctx.info(f"Previewing impact of ingredient: {ingredient_name}")

                conn = get_db_connection()
                try:
                    # Portable cutoff: compute in Python and bind, instead of the
                    # SQLite-only date('now', '-90 days') (no such function on PG).
                    from datetime import datetime, timedelta

                    cutoff_date = (datetime.now() - timedelta(days=90)).strftime(
                        "%Y-%m-%d"
                    )
                    cursor = conn.execute(
                        """
                        SELECT DISTINCT p.product_id, p.description, p.brand
                        FROM products p
                        JOIN purchase_events pe ON p.product_id = pe.product_id
                        WHERE pe.event_date >= ?
                        LIMIT 500
                        """,
                        (cutoff_date,),
                    )
                    products = cursor.fetchall()

                    import re

                    matched_products = []
                    pattern = re.compile(r"\b" + re.escape(ingredient_name) + r"\b", re.IGNORECASE)

                    for product in products:
                        text = f"{product['description']} {product['brand'] or ''}".lower()
                        if pattern.search(text):
                            matched_products.append(
                                {
                                    "product_id": product["product_id"],
                                    "description": product["description"],
                                    "brand": product["brand"],
                                }
                            )

                    return {
                        "success": True,
                        "ingredient_name": ingredient_name,
                        "severity": severity,
                        "total_products_checked": len(products),
                        "would_flag_count": len(matched_products),
                        "percentage": (
                            round(len(matched_products) / len(products) * 100, 1) if products else 0
                        ),
                        "sample_products": matched_products[:10],
                    }

                except Exception as e:
                    return {"success": False, "error": f"Preview failed: {str(e)}"}
                finally:
                    conn.close()

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
