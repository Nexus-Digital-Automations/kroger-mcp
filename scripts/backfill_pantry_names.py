#!/usr/bin/env python3
"""
One-shot backfill for pantry_items rows whose ``description`` is NULL/empty.

Existing rows display as raw product IDs on ``/pantry`` because earlier
versions of the cart-add code paths did not persist a description. This
script restores names in-place — products table first (free), Kroger
product API second (one call per still-missing row) — and also writes
the recovered name back to ``products`` so ``add_to_pantry``'s built-in
fallback works for future cold-start callers.

Idempotent: subsequent runs do zero work if all rows already have a name.

Owner: kroger_mcp / analytics
@internal — maintenance script, not part of the runtime API.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from kroger_mcp.analytics.database import ensure_initialized, get_db_cursor
from kroger_mcp.auth.dependencies import default_user_id
from kroger_mcp.tools.shared import get_client_credentials_client, get_preferred_location_id

logger = logging.getLogger("backfill_pantry_names")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _nameless_product_ids() -> list[str]:
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT product_id FROM pantry_items "
            "WHERE description IS NULL OR TRIM(description) = ''"
        )
        return [row["product_id"] for row in cursor.fetchall()]


def _fill_from_products_table(product_ids: list[str]) -> tuple[int, list[str]]:
    """Returns (filled_count, still_missing_ids)."""
    filled = 0
    still_missing: list[str] = []
    now = datetime.now().isoformat()
    with get_db_cursor() as cursor:
        for pid in product_ids:
            cursor.execute(
                "SELECT description FROM products WHERE product_id = ? "
                "AND description IS NOT NULL AND TRIM(description) != ''",
                (pid,),
            )
            row = cursor.fetchone()
            if row and row["description"]:
                cursor.execute(
                    "UPDATE pantry_items SET description = ?, last_updated_at = ? "
                    "WHERE product_id = ?",
                    (row["description"], now, pid),
                )
                filled += 1
            else:
                still_missing.append(pid)
    return filled, still_missing


def _fetch_kroger_description(client, product_id: str, location_id: str | None) -> str | None:
    try:
        result = client.product.get_product(product_id=product_id, location_id=location_id)
    except Exception as exc:
        logger.warning("Kroger lookup failed for %s: %s", product_id, exc)
        return None
    payload = (result or {}).get("data") or {}
    desc = payload.get("description")
    return desc.strip() if isinstance(desc, str) and desc.strip() else None


def _fill_from_kroger(product_ids: list[str]) -> tuple[int, list[str]]:
    """Returns (filled_count, still_missing_ids)."""
    if not product_ids:
        return 0, []
    # Global maintenance script with no per-request caller; product
    # descriptions/credentials it resolves are shared catalog data, not
    # per-user, so the migration default owner is the right stand-in (same
    # pattern as web/app.py's startup pattern-cache warm-up).
    owner = default_user_id()
    client = get_client_credentials_client(owner)
    location_id = get_preferred_location_id(owner)
    filled = 0
    still_missing: list[str] = []
    now = datetime.now().isoformat()
    for pid in product_ids:
        description = _fetch_kroger_description(client, pid, location_id)
        if not description:
            still_missing.append(pid)
            continue
        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO products (product_id, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(product_id) DO UPDATE SET "
                "description = COALESCE(excluded.description, products.description), "
                "updated_at = excluded.updated_at",
                (pid, description, now, now),
            )
            cursor.execute(
                "UPDATE pantry_items SET description = ?, last_updated_at = ? "
                "WHERE product_id = ?",
                (description, now, pid),
            )
        filled += 1
    return filled, still_missing


def main() -> int:
    ensure_initialized()

    nameless = _nameless_product_ids()
    if not nameless:
        logger.info("Pantry backfill: 0 rows need a name. Nothing to do.")
        return 0

    logger.info("Pantry backfill: %d nameless rows.", len(nameless))

    filled_local, still_missing = _fill_from_products_table(nameless)
    logger.info("Filled %d from local products table.", filled_local)

    filled_remote, unresolved = _fill_from_kroger(still_missing)
    logger.info("Filled %d via Kroger product API.", filled_remote)

    if unresolved:
        logger.warning(
            "Could not resolve %d product_id(s): %s",
            len(unresolved),
            ", ".join(unresolved[:10]) + (" …" if len(unresolved) > 10 else ""),
        )

    logger.info(
        "Pantry backfill complete: %d total updated, %d still nameless.",
        filled_local + filled_remote,
        len(unresolved),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
