"""Spec tests for analytics.ingredients.is_spice — gates spices in the cart preview."""

import pytest

from kroger_mcp.analytics.ingredients import is_spice


@pytest.mark.parametrize(
    "name",
    [
        "basil",
        "Basil",
        "fresh basil",
        "ground cumin",
        "cumin",
        "salt",
        "Sea Salt",
        "kosher salt",
        "freshly ground black pepper",
        "black pepper",
        "white pepper",
        "ground pepper",
        "whole peppercorns",
        "crushed red pepper",
        "red pepper flakes",
        "smoked paprika",
        "paprika (sweet)",
        "ground cinnamon",
        "bay leaves",
        "soy sauce",
        "rice vinegar",
        "fresh thyme leaves",
    ],
)
def test_classifies_common_seasonings_as_spices(name: str) -> None:
    assert is_spice(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "salted butter",  # \b prevents matching "salt" inside "salted"
        "sugar",
        "flour",
        "all-purpose flour",
        "chicken breast",
        "ground beef",
        "olive oil",
        "bell pepper",  # standalone "pepper" intentionally excluded
        "red bell pepper",
        "green pepper",
        "jalapeño pepper",
        "milk",
        "heavy cream",
        "yellow onion",
        "garlic",  # not in the herb/spice alias list
        "tomato",
    ],
)
def test_does_not_classify_non_spices_as_spices(name: str) -> None:
    assert is_spice(name) is False


@pytest.mark.parametrize("name", ["", None])
def test_returns_false_for_empty_input(name: str | None) -> None:
    assert is_spice(name) is False
