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
        # Expanded lexicon — the long tail that used to be missed.
        "garlic powder",
        "onion powder",
        "garlic salt",
        "celery salt",
        "curry powder",
        "garam masala",
        "italian seasoning",
        "taco seasoning",
        "cajun seasoning",
        "jerk seasoning",
        "poultry seasoning",
        "pumpkin pie spice",
        "herbes de provence",
        "za'atar",
        "sumac",
        "allspice",
        "mace",
        "marjoram",
        "mustard seed",
        "fennel seed",
        "coriander seed",
        "star anise",
        "fenugreek",
        "vanilla extract",
        "lemon pepper",
        "old bay",
        "chinese five spice",
        "seasoned salt",
        "dried oregano",
        "ground cloves",
        "smoked paprika seasoning",  # blend regex
    ],
)
def test_classifies_common_seasonings_as_spices(name: str) -> None:
    assert is_spice(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "house spice blend",
        "smoky seasoning blend",
        "cajun spice mix",
    ],
)
def test_blend_regex_classifies_named_blends(name: str) -> None:
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
        "garlic",  # bare aromatic — only "garlic powder"/"garlic salt" are spices
        "onion",  # bare aromatic
        "tomato",
        # Hardening: the expanded lexicon must NOT introduce these false positives.
        "baking powder",  # no bare ".*powder" rule
        "cocoa powder",
        "protein powder",
        "vanilla yogurt",  # must not match "vanilla extract"
        "vanilla ice cream",
        "seasoned chicken breast",  # "seasoned" ≠ "seasoning"
    ],
)
def test_does_not_classify_non_spices_as_spices(name: str) -> None:
    assert is_spice(name) is False


@pytest.mark.parametrize("name", ["", None])
def test_returns_false_for_empty_input(name: str | None) -> None:
    assert is_spice(name) is False
