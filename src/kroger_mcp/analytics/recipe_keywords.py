"""Keyword vocabularies and precomputed matchers for recipe health scoring.

Pure data module: the healthy-category keyword lists, penalty tables, and
ingredient-name signal lists used by ``recipe_scoring._calculate_health_score``,
plus the module-level frozensets derived from them ONCE at import (rather than
rebuilt per scoring call).

Matching semantics are substring-based on purpose: every check in the scorer is
a substring test (``kw in name``) because the vocabularies rely on it — e.g.
"bean" matches "beans"/"green beans", "berry" matches "blueberry", "cranberr"
matches "cranberries", and multi-word entries like "sweet potato" / "cream of" /
"grass-fed" are inherently substrings. Do NOT switch to whole-word set
intersection; it would change scores.
"""

# ---------------------------------------------------------------------------
# Healthy category keyword matching
# ---------------------------------------------------------------------------

HEALTHY_CATEGORIES: dict[str, list[str]] = {
    "produce": [
        "vegetable",
        "spinach",
        "kale",
        "broccoli",
        "carrot",
        "tomato",
        "onion",
        "garlic",
        "pepper",
        "lettuce",
        "cucumber",
        "zucchini",
        "asparagus",
        "apple",
        "banana",
        "berry",
        "lemon",
        "lime",
        "celery",
        "mushroom",
        "peas",
        "squash",
        "eggplant",
        "cabbage",
        "bok choy",
        "sweet potato",
        "potato",
        "orange",
        "cranberr",
        "mango",
        "pineapple",
        "grapefruit",
        "scallion",
        "radish",
        "beet",
        "artichoke",
        "corn",
        "romaine",
        "arugula",
        "chard",
        "watermelon",
        "pear",
        "peach",
        "plum",
        "jalapeno",
        "jalapeño",
        "leek",
        "shallot",
        "okra",
        "kohlrabi",
        "endive",
        "fennel bulb",
    ],
    "lean_protein": [
        "chicken",
        "turkey",
        "salmon",
        "tuna",
        "egg",
        "lentil",
        "chickpea",
        "black bean",
        "kidney bean",
        "tofu",
        "tempeh",
        "shrimp",
        "cod",
        "tilapia",
        "fish",
        "clam",
        "bean",
        "halibut",
        "sardine",
        "trout",
        "mackerel",
        "anchovy",
        "mussel",
        "oyster",
        "crab",
        "lobster",
        "scallop",
        "pork tenderloin",
        "sirloin",
        "flank steak",
        "edamame",
        "hummus",
    ],
    "whole_grain": [
        "brown rice",
        "quinoa",
        "oats",
        "whole wheat",
        "whole grain",
        "farro",
        "barley",
        "bulgur",
        "wild rice",
        "buckwheat",
        "millet",
        "steel-cut oats",
        "rolled oats",
        "whole-wheat pasta",
        "whole wheat pasta",
    ],
    "healthy_fat": [
        "olive oil",
        "avocado",
        "almond",
        "walnut",
        "cashew",
        "flaxseed",
        "chia",
        "hemp seed",
        "pecan",
        "pine nut",
        "pistachio",
        "sesame seed",
        "pumpkin seed",
        "sunflower seed",
        "tahini",
        "avocado oil",
        "coconut oil",
    ],
    "herbs_spices": [
        "basil",
        "oregano",
        "thyme",
        "rosemary",
        "cilantro",
        "parsley",
        "mint",
        "dill",
        "cumin",
        "turmeric",
        "ginger",
        "cinnamon",
        "paprika",
        "cayenne",
        "sage",
        "bay leaf",
        "coriander",
        "cardamom",
        "saffron",
        "nutmeg",
        "clove",
        "chive",
        "fennel",
        "tarragon",
        "five-spice",
        "chili powder",
        "smoked paprika",
        "garlic powder",
        "onion powder",
        "red pepper flake",
        "italian seasoning",
        "za'atar",
        "old bay",
    ],
}

# Penalty caps per severity
_PENALTY_CAPS = {
    "critical": 45,
    "warning": 24,
    "watch": 12,
}

# Per-match penalty per severity
_PENALTY_PER_MATCH = {
    "critical": 15,
    "warning": 8,
    "watch": 3,
}

# ---------------------------------------------------------------------------
# Build-up scoring: ingredient-name signal keywords
# ---------------------------------------------------------------------------

WHOLE_FOOD_SIGNALS: list[str] = [
    "fresh",
    "whole",
    "organic",
    "raw",
    "grass-fed",
    "grass fed",
    "wild-caught",
    "wild caught",
    "bone-in",
    "skin-on",
]

PROCESSED_INDICATORS: list[str] = [
    "cream of",
    "condensed",
    "pre-made",
    "pre-packaged",
    "store-bought",
    "cooking spray",
    "liquid smoke",
    "instant",
]

CONVENIENCE_INDICATORS: list[str] = [
    "rotisserie",
    "canned",
    "breadcrumbs",
    "panko",
    "marinara sauce",
    "curry paste",
    "better than bouillon",
]

HEAVY_NEGATIVES: list[str] = [
    "bacon",
    "sausage",
    "andouille",
]

SUGAR_KEYWORDS: list[str] = [
    "brown sugar",
    "powdered sugar",
    "corn syrup",
    "sugar",
]

# Neutral cooking staples — present in almost every recipe but not "unhealthy".
# Excluded from the quality-ratio denominator so they don't drag the score
# down when paired with otherwise-healthy ingredients.
NEUTRAL_STAPLES: list[str] = [
    "salt",
    "pepper",
    "black pepper",
    "white pepper",
    "water",
    "ice",
    "vinegar",
    "rice vinegar",
    "balsamic",
    "apple cider vinegar",
    "soy sauce",
    "tamari",
    "fish sauce",
    "worcestershire",
    "broth",
    "stock",
    "bouillon",
    "lemon juice",
    "lime juice",
    "orange juice",
    "mustard",
    "dijon",
    "ketchup",
    "hot sauce",
    "sriracha",
    "tabasco",
    "honey",
    "maple syrup",
    "yogurt",
    "greek yogurt",
    "milk",
    "butter",
    "cream",
    "cheese",
    "flour",
    "cornstarch",
    "baking soda",
    "baking powder",
    "yeast",
    "egg white",
    "egg yolk",
    "vanilla",
    "vanilla extract",
]


# ---------------------------------------------------------------------------
# Precomputed matchers (built ONCE at import, not per scoring call).
# ---------------------------------------------------------------------------

# Flattened, de-duplicated healthy keywords for the quality-ratio scan.
ALL_HEALTHY_KEYWORDS: frozenset[str] = frozenset(
    kw for kws in HEALTHY_CATEGORIES.values() for kw in kws
)

# Per-category keyword sets for category-coverage detection (preserves order of
# detection via HEALTHY_CATEGORIES iteration; membership uses these sets).
HEALTHY_CATEGORY_KEYWORDS: dict[str, frozenset[str]] = {
    cat: frozenset(kws) for cat, kws in HEALTHY_CATEGORIES.items()
}

WHOLE_FOOD_SIGNAL_SET: frozenset[str] = frozenset(WHOLE_FOOD_SIGNALS)
PROCESSED_INDICATOR_SET: frozenset[str] = frozenset(PROCESSED_INDICATORS)
CONVENIENCE_INDICATOR_SET: frozenset[str] = frozenset(CONVENIENCE_INDICATORS)
HEAVY_NEGATIVE_SET: frozenset[str] = frozenset(HEAVY_NEGATIVES)
SUGAR_KEYWORD_SET: frozenset[str] = frozenset(SUGAR_KEYWORDS)
NEUTRAL_STAPLE_SET: frozenset[str] = frozenset(NEUTRAL_STAPLES)
