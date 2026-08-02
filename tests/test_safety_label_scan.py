"""Regression tests: the safety check must scan a product's label, not its name.

Guards the 2026-08-02 finding that `safety(action='check_product')` graded
Kroger condensed cream of chicken soup 0001111016044 at 95/A with zero flagged
ingredients, while `recipes(action='analyze')` on the same product_id flagged
seven additives. The safety path scanned only the product description -- a name
never lists its own additives, and the wholesome words in it ("cream",
"chicken") earned positive-attribute bonuses on top.
"""

from __future__ import annotations

import pytest

from kroger_mcp.analytics import safety as safety_mod
from kroger_mcp.analytics.ingredients import check_product_safety, resolve_scan_text
from kroger_mcp.analytics.safety import checks as checks_mod

# The real label for the soup in the finding, trimmed to the flagged additives.
DIRTY_LABEL = (
    "chicken broth, modified food starch, soy protein isolate, wheat flour, "
    "cream, maltodextrin, soy lecithin, autolyzed yeast extract, natural flavors"
)
CLEAN_NAME = "Kroger Cream of Chicken Condensed Soup"
PRODUCT_ID = "0001111016044"


class TestResolveScanText:
    """`resolve_scan_text` picks the authoritative text to scan."""

    def test_prefers_label_over_name(self):
        assert resolve_scan_text(CLEAN_NAME, "Kroger", DIRTY_LABEL) == DIRTY_LABEL

    def test_falls_back_to_brand_and_description_when_no_label(self):
        assert resolve_scan_text("Cream of Chicken", "Kroger", None) == (
            "Kroger Cream of Chicken"
        )

    def test_falls_back_to_description_when_no_brand_and_no_label(self):
        assert resolve_scan_text("Cream of Chicken", None, None) == "Cream of Chicken"

    def test_empty_label_is_treated_as_absent(self):
        """An empty ingredients_text column must not blank out the scan."""
        assert resolve_scan_text("Cream of Chicken", None, "") == "Cream of Chicken"


class TestNameCleanLabelDirty:
    """The exact product from the finding must no longer score as clean."""

    def test_name_clean_label_dirty_is_flagged(self, monkeypatch):
        monkeypatch.setattr(
            checks_mod, "_load_ingredients_text", lambda ids: {PRODUCT_ID: DIRTY_LABEL}
        )

        status = safety_mod.get_product_safety_status(
            product_id=PRODUCT_ID,
            description=CLEAN_NAME,
            brand="Kroger",
            user_id="label-scan-user",
        )

        result = status.safety_result
        assert result is not None
        assert result.has_concerns, "label lists additives but product scored clean"
        assert result.matches, "no ingredient matches found in a label full of them"

    def test_name_only_scan_would_have_missed_it(self):
        """Pins the old behavior, so this test fails if the bug returns."""
        name_only = check_product_safety(CLEAN_NAME, user_id="label-scan-user")
        label = check_product_safety(DIRTY_LABEL, user_id="label-scan-user")

        assert not name_only.matches, "fixture assumption: the name looks clean"
        assert label.matches, "fixture assumption: the label does not"
        assert label.score < name_only.score


class TestBothPathsAgree:
    """The safety path and the recipe path must not disagree about one product."""

    def test_agrees_with_recipe_path(self):
        """Both paths route through `resolve_scan_text`, so both see the label."""
        safety_text = resolve_scan_text(CLEAN_NAME, "Kroger", DIRTY_LABEL)
        recipe_text = resolve_scan_text(CLEAN_NAME, "Kroger", DIRTY_LABEL)

        safety_flags = {m.ingredient_key for m in check_product_safety(
            safety_text, user_id="label-scan-user"
        ).matches}
        recipe_flags = {m.ingredient_key for m in check_product_safety(
            recipe_text, user_id="label-scan-user"
        ).matches}

        assert safety_flags == recipe_flags
        assert safety_flags, "both paths agreed on nothing -- fixture is inert"


class TestNoLabelUnchanged:
    """Products without a cached label keep scanning exactly as they did."""

    def test_no_label_unchanged(self, monkeypatch):
        monkeypatch.setattr(checks_mod, "_load_ingredients_text", lambda ids: {})

        status = safety_mod.get_product_safety_status(
            product_id="no-label-product",
            description="Organic Baby Spinach",
            brand="Simple Truth",
            user_id="label-scan-user",
        )

        expected = check_product_safety(
            resolve_scan_text("Organic Baby Spinach", "Simple Truth", None),
            brand="Simple Truth",
            user_id="label-scan-user",
        )
        assert status.safety_result is not None
        assert status.safety_result.score == expected.score


class TestLookupFailureDegrades:
    """A label-lookup failure must never fail the safety check itself."""

    def test_degrades_to_description_when_lookup_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(checks_mod, "get_db_cursor", _boom)

        assert checks_mod._load_ingredients_text([PRODUCT_ID]) == {}

    def test_degrades_without_querying_when_no_ids(self, monkeypatch):
        def _should_not_run():
            raise AssertionError("queried the DB for an empty id list")

        monkeypatch.setattr(checks_mod, "get_db_cursor", _should_not_run)

        assert checks_mod._load_ingredients_text([]) == {}
        assert checks_mod._load_ingredients_text(["", ""]) == {}


@pytest.mark.parametrize(
    "additive",
    ["soy protein isolate", "maltodextrin", "autolyzed yeast extract"],
)
def test_known_additives_are_detected_in_label_text(additive):
    """Each additive named in the finding is actually matchable."""
    result = check_product_safety(
        f"water, salt, {additive}, citric acid", user_id="label-scan-user"
    )
    assert result.matches, f"{additive!r} produced no match"
