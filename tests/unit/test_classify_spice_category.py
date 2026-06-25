"""Spec tests for the authoritative (Kroger category/aisle) spice signal.

classify_spice trusts a linked product's category_type / aisle over the name, so
a product the name alone wouldn't catch ("House Blend") is still gated as a spice
when Kroger files it under Seasonings & Spices.
"""

from kroger_mcp.analytics.ingredients import (
    category_type_from_aisles,
    classify_spice,
)


def test_category_type_signal_wins_over_unmatched_name() -> None:
    assert classify_spice("House Blend", category_type="spice") is True
    assert classify_spice("House Blend", category_type="seasonings") is True


def test_aisle_description_signal_wins_over_unmatched_name() -> None:
    assert (
        classify_spice("House Blend", aisle_descriptions=["Seasonings & Spices"])
        is True
    )


def test_non_spice_category_falls_through_to_name() -> None:
    # A non-spice category must NOT force True — it falls through to name matching,
    # because category_type defaults to 'uncategorized' for most rows.
    assert classify_spice("chicken breast", category_type="uncategorized") is False
    assert classify_spice("cumin", category_type="produce") is True  # name still wins


def test_name_only_unmatched_blend_is_not_spice() -> None:
    assert classify_spice("House Blend") is False


def test_category_type_from_aisles_mapping() -> None:
    assert category_type_from_aisles(["Seasonings & Spices"]) == "spice"
    assert category_type_from_aisles(["Fresh Herbs"]) == "spice"
    assert category_type_from_aisles(["Produce"]) is None
    assert category_type_from_aisles([]) is None
    assert category_type_from_aisles(None) is None
