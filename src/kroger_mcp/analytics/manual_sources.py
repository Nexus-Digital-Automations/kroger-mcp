"""Vendor attribution for items the user sources themselves.

Owns the three things every surface needs to agree on for a non-Kroger item:

1. `is_manual_item` -- the single predicate for "this can never be sent to the
   Kroger cart API". Manual status is *derived* from a falsy `product_id`, not
   declared by a flag, so a caller cannot produce a row that is simultaneously
   unorderable and unmarked.
2. `normalize_source` -- collapses the many spellings of a known vendor onto one
   canonical name so the groups don't fragment, while letting any unknown
   vendor through untouched.
3. `group_by_source` -- turns a flat manual-item list into per-vendor sections,
   which is what makes a shopping list readable as an errand plan.

The persisted column is `manual_source`; the wire/JSON field is `source`.
`user_shopping_lists` already has a `recipe_source` column meaning "which recipe
did this come from", so a bare `source` column there would be ambiguous.

@stable
"""

from typing import Any

# Absent or blank vendor. Sorts last in `group_by_source` -- "I have to buy this
# myself but haven't said where" is the least actionable section of the list.
UNSPECIFIED_SOURCE = "Manual"

# Alias -> canonical spelling. Lookup is on the casefolded, punctuation-stripped
# form of the input (see `_alias_key`), so only genuinely distinct spellings
# need an entry here -- "Wal-Mart", "wal mart" and "WALMART" all reduce to
# "walmart" on their own.
KNOWN_SOURCES: dict[str, str] = {
    "walmart": "Walmart",
    "costco": "Costco",
    "amazon": "Amazon",
    "amazonfresh": "Amazon",
    "wholefoods": "Whole Foods",
    "traderjoes": "Trader Joe's",
    "target": "Target",
    "samsclub": "Sam's Club",
    "sams": "Sam's Club",
    "aldi": "Aldi",
    "heb": "H-E-B",
}


def _alias_key(source: str) -> str:
    """Reduce a vendor spelling to its lookup key: letters and digits only."""
    return "".join(char for char in source.casefold() if char.isalnum())


def normalize_source(source: str | None) -> str:
    """Return the canonical vendor name for a free-text source.

    Known vendors collapse onto one spelling so their groups don't fragment
    ("wal-mart" and "Walmart" are the same errand). Unknown vendors pass
    through with whitespace collapsed but capitalization intact, so a user's
    "Indian grocery on Airport Blvd" survives verbatim. Never raises and never
    rejects -- an unrecognized vendor is a normal, supported case.
    """
    if not source or not source.strip():
        return UNSPECIFIED_SOURCE
    collapsed = " ".join(source.split())
    return KNOWN_SOURCES.get(_alias_key(collapsed), collapsed)


def stored_source(source: str | None) -> str | None:
    """Canonical vendor for the `manual_source` column, or None if none was named.

    `normalize_source` substitutes the UNSPECIFIED_SOURCE sentinel for a blank
    vendor, which is right for display and grouping but wrong for storage:
    persisting it would make "the user never said where" indistinguishable from
    a store literally called "Manual", and it would shadow the `override_reason`
    fallback in `manual_note` -- the only explanation older rows carry.
    """
    return normalize_source(source) if source and source.strip() else None


def is_known_source(source: str | None) -> bool:
    """True when `source` names a vendor with a canonical spelling."""
    return source is not None and _alias_key(source) in KNOWN_SOURCES


def is_manual_item(item: dict[str, Any]) -> bool:
    """True when `item` cannot be ordered from Kroger and must be sourced by hand.

    Two ways an item qualifies, and both must stay covered:

    - No `product_id` at all -- an ingredient the user never linked. This is the
      common case and the reason the field is optional.
    - A synthetic `manual:<uuid>` id -- a manual favorite re-posted by a caller
      that round-trips stored ids (favorites "Move to List" does exactly this).

    Callers must treat a True result as un-bypassable. A manual item has no UPC,
    so Kroger would reject it anyway, but the real point is that the user said
    they are buying it somewhere else.
    """
    from .favorites import is_manual_product_id

    product_id = item.get("product_id")
    return not product_id or is_manual_product_id(product_id)


def item_source(item: dict[str, Any]) -> str:
    """Canonical vendor for one item, reading either the wire or column name."""
    return normalize_source(item.get("source") or item.get("manual_source"))


def manual_note(item: dict[str, Any]) -> str | None:
    """The one-line human explanation for a manual item, or None if there is none.

    A named vendor is the most actionable thing we can say, so it wins. Failing
    that, fall back to whatever free-text reason the caller supplied: manual
    favorites created before `source` existed carry only `override_reason`
    ("Farmers market only"), and overwriting that with a generic line would
    throw away the sole explanation the user ever wrote down.

    Returns None rather than a placeholder when neither exists — `source` and
    `manual_purchase` already say the item is an errand, so an empty note is
    honest, not a gap.
    """
    source = item.get("source") or item.get("manual_source")
    if source and source.strip():
        return f"Buy at {normalize_source(source)}"
    return item.get("override_reason") or None


def _section_order(source: str) -> tuple[int, str]:
    """Known vendors first, then named unknowns, then unattributed items."""
    if source == UNSPECIFIED_SOURCE:
        return (2, "")
    return (0 if is_known_source(source) else 1, source.casefold())


def group_by_source(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group manual items into per-vendor sections for display.

    Returns `[{"source", "item_count", "items"}, ...]`. Items keep their own
    dict shape; only the grouping is added, so each surface can put whatever
    fields it already emits into the sections. An empty input yields an empty
    list rather than a single empty section.
    """
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        sections.setdefault(item_source(item), []).append(item)

    return [
        {
            "source": source,
            "item_count": len(grouped),
            "items": grouped,
        }
        for source, grouped in sorted(sections.items(), key=lambda kv: _section_order(kv[0]))
    ]
