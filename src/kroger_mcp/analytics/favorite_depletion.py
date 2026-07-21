"""
Favorite-list-derived pantry depletion rates.

Split out of favorites.py/pantry.py (both already oversized) so pantry
depletion can respect a product's favorites cadence — e.g. an item on a
weekly favorites list should drain roughly weekly — without growing either
file further.
"""

from ._user_scope import resolve_user_id as _resolve_user_id
from .database import ensure_initialized, get_db_cursor


def get_favorite_depletion_rates(user_id: str) -> dict[str, float]:
    """Derive an implied daily pantry depletion rate (percent/day) per product
    from the cadence of the favorite list(s) it belongs to.

    Cadence source per product: `favorite_list_items.typical_gap_days` (the
    account's learned reorder gap) when set, otherwise the parent
    `favorite_lists.reorder_weeks * 7`. A product on multiple lists with
    different cadences uses the shortest one (fastest drain), since that's
    the more conservative "don't run out" estimate. Products with no
    cadence data on either side are omitted.

    Args:
        user_id: Owner. None resolves to the migration-installed default user.

    Returns:
        Dict of product_id -> daily depletion rate (0-100).
    """
    ensure_initialized()
    owner = _resolve_user_id(user_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT fli.product_id, fli.typical_gap_days, fl.reorder_weeks
            FROM favorite_list_items fli
            JOIN favorite_lists fl ON fli.list_id = fl.id
            WHERE fl.user_id = ?
              AND (fli.typical_gap_days IS NOT NULL OR fl.reorder_weeks IS NOT NULL)
            """,
            (owner,),
        )
        rows = cursor.fetchall()

    rates: dict[str, float] = {}
    for row in rows:
        cadence_days = row["typical_gap_days"] or (
            row["reorder_weeks"] * 7 if row["reorder_weeks"] else None
        )
        if not cadence_days or cadence_days <= 0:
            continue
        rate = 100.0 / cadence_days
        existing = rates.get(row["product_id"])
        if existing is None or rate > existing:
            rates[row["product_id"]] = rate
    return rates
