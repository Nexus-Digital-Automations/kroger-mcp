"""
Bad ingredients detection for product safety filtering.

This module contains an evidence-based list of ingredients to avoid, designed
to optimize for:

1. GENERAL HEALTH - Avoiding additives linked to chronic disease outcomes
2. CANCER PREVENTION - Flagging IARC-classified carcinogens and genotoxic additives
3. METABOLIC HEALTH - Identifying ingredients that spike blood sugar or disrupt
   insulin response
4. MICROBIOME OPTIMIZATION - Flagging emulsifiers and sweeteners with consistent
   gut-barrier disruption or dysbiosis evidence in human/animal studies
5. MINIMIZING ULTRA-PROCESSED FOODS - Detecting markers of heavy industrial
   processing (emulsifiers, artificial colors, synthetic preservatives)

Severity levels are assigned based on strength of evidence:
- CRITICAL: Strong human outcome evidence (IARC classifications, FDA actions,
  EFSA safety concerns) or consistent mechanistic evidence at real-world exposures
- WARNING: Moderate evidence of harm, regulatory concern in some jurisdictions,
  or consistent microbiome disruption signals
- WATCH: Markers of ultra-processing or ingredients to minimize for optimal health,
  even if not directly harmful

Sources include: WHO/IARC, EFSA, FDA, peer-reviewed studies on gut microbiome,
and systematic reviews on ultra-processed food consumption.
"""

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Severity levels for flagged ingredients."""

    CRITICAL = "critical"  # Hard warnings - strongly recommend avoiding
    WARNING = "warning"  # Soft warnings - moderate concern
    WATCH = "watch"  # Informational - low concern


@dataclass
class IngredientInfo:
    """Information about a flagged ingredient."""

    key: str  # Unique identifier
    name: str  # Display name
    aliases: list[str]  # Alternative names/spellings
    severity: Severity  # Severity level
    reason: str  # Why it's flagged
    category: str  # Category (preservative, sweetener, etc.)
    exclude_patterns: list[str] | None = None  # Patterns to exclude (e.g., "sugar free")


# Comprehensive list of bad ingredients with severity levels
BAD_INGREDIENTS: list[IngredientInfo] = [
    # ==================== CRITICAL SEVERITY ====================
    # Preservatives linked to serious health concerns
    IngredientInfo(
        key="sodium_nitrite",
        name="Sodium Nitrite",
        aliases=["sodium nitrate", "nitrite", "nitrate", "cured with nitrite"],
        severity=Severity.CRITICAL,
        reason="Linked to increased cancer risk when heated",
        category="preservative",
    ),
    IngredientInfo(
        key="bha",
        name="BHA (Butylated Hydroxyanisole)",
        aliases=["butylated hydroxyanisole", "E320"],
        severity=Severity.CRITICAL,
        reason="Potential carcinogen, endocrine disruptor",
        category="preservative",
    ),
    IngredientInfo(
        key="bht",
        name="BHT (Butylated Hydroxytoluene)",
        aliases=["butylated hydroxytoluene", "E321"],
        severity=Severity.CRITICAL,
        reason="Potential carcinogen",
        category="preservative",
    ),
    IngredientInfo(
        key="potassium_bromate",
        name="Potassium Bromate",
        aliases=["bromate", "bromated flour"],
        severity=Severity.CRITICAL,
        reason="Banned in many countries, potential carcinogen",
        category="preservative",
    ),
    IngredientInfo(
        key="bvo",
        name="Brominated Vegetable Oil",
        aliases=["BVO", "brominated oil"],
        severity=Severity.CRITICAL,
        reason="Neurological and reproductive concerns",
        category="emulsifier",
    ),
    # Artificial sweeteners with significant concerns
    IngredientInfo(
        key="aspartame",
        name="Aspartame",
        aliases=["equal", "nutrasweet", "E951", "APM"],
        severity=Severity.CRITICAL,
        reason="Neurological concerns, potential carcinogen (WHO)",
        category="artificial_sweetener",
    ),
    # Unhealthy fats
    IngredientInfo(
        key="trans_fat",
        name="Trans Fat / Partially Hydrogenated Oil",
        aliases=[
            "partially hydrogenated",
            "PHO",
            "trans fat",
            "partially hydrogenated oil",
            "hydrogenated vegetable oil",
        ],
        severity=Severity.CRITICAL,
        reason="Heart disease, stroke risk",
        category="fat",
    ),
    # High fructose corn syrup
    IngredientInfo(
        key="hfcs",
        name="High Fructose Corn Syrup",
        aliases=["HFCS", "corn syrup high fructose", "glucose-fructose syrup"],
        severity=Severity.CRITICAL,
        reason="Linked to obesity, diabetes, metabolic syndrome",
        category="sweetener",
    ),
    # Hydrolyzed protein (contains free glutamate)
    IngredientInfo(
        key="hydrolyzed_protein",
        name="Hydrolyzed Protein",
        aliases=[
            "hydrolyzed vegetable protein",
            "HVP",
            "hydrolyzed soy protein",
            "hydrolyzed plant protein",
            "hydrolyzed yeast",
            "protein hydrolysate",
            "hydrolyzed wheat protein",
            "hydrolyzed corn protein",
        ],
        severity=Severity.CRITICAL,
        reason="Contains free glutamate, excitotoxin, headaches",
        category="flavor_enhancer",
    ),
    # Sulfites
    IngredientInfo(
        key="sulfites",
        name="Sulfites",
        aliases=[
            "sodium sulfite",
            "sodium bisulfite",
            "sodium metabisulfite",
            "potassium bisulfite",
            "potassium metabisulfite",
            "sulfur dioxide",
            "E220",
            "E221",
            "E222",
            "E223",
            "E224",
            "E225",
            "E226",
            "E227",
            "E228",
        ],
        severity=Severity.CRITICAL,
        reason="Severe allergic reactions, asthma trigger, banned on fresh produce",
        category="preservative",
    ),
    IngredientInfo(
        key="propyl_gallate",
        name="Propyl Gallate",
        aliases=["E310", "propyl 3,4,5-trihydroxybenzoate"],
        severity=Severity.CRITICAL,
        reason="Potential carcinogen, endocrine disruptor",
        category="preservative",
    ),
    IngredientInfo(
        key="interesterified_fat",
        name="Interesterified Fat",
        aliases=["interesterified oil", "interesterified vegetable oil"],
        severity=Severity.CRITICAL,
        reason="May raise blood sugar, heart disease concerns",
        category="fat",
    ),
    IngredientInfo(
        key="olestra",
        name="Olestra",
        aliases=["olean"],
        severity=Severity.CRITICAL,
        reason="Causes digestive issues, blocks vitamin absorption",
        category="fat_substitute",
    ),
    # ==================== WARNING SEVERITY ====================
    # Artificial colors
    IngredientInfo(
        key="red_40",
        name="Red 40 (Allura Red)",
        aliases=["allura red", "FD&C red 40", "E129", "red dye 40", "red #40", "red 40"],
        severity=Severity.WARNING,
        reason="Behavioral effects in children, potential carcinogen",
        category="artificial_color",
    ),
    IngredientInfo(
        key="red_3",
        name="Red 3 (Erythrosine)",
        aliases=["erythrosine", "FD&C red 3", "E127", "red dye 3", "red #3"],
        severity=Severity.WARNING,
        reason="Thyroid tumors in animals, banned in cosmetics",
        category="artificial_color",
    ),
    IngredientInfo(
        key="yellow_5",
        name="Yellow 5 (Tartrazine)",
        aliases=["tartrazine", "FD&C yellow 5", "E102", "yellow dye 5", "yellow #5", "yellow 5"],
        severity=Severity.WARNING,
        reason="Behavioral effects, allergic reactions",
        category="artificial_color",
    ),
    IngredientInfo(
        key="yellow_6",
        name="Yellow 6 (Sunset Yellow)",
        aliases=["sunset yellow", "FD&C yellow 6", "E110", "yellow dye 6", "yellow #6", "yellow 6"],
        severity=Severity.WARNING,
        reason="Behavioral effects in children",
        category="artificial_color",
    ),
    IngredientInfo(
        key="blue_1",
        name="Blue 1 (Brilliant Blue)",
        aliases=["brilliant blue", "FD&C blue 1", "E133", "blue dye 1", "blue #1", "blue 1"],
        severity=Severity.WARNING,
        reason="Potential neurotoxin",
        category="artificial_color",
    ),
    IngredientInfo(
        key="blue_2",
        name="Blue 2 (Indigo Carmine)",
        aliases=["indigo carmine", "FD&C blue 2", "E132", "blue dye 2", "blue #2"],
        severity=Severity.WARNING,
        reason="Brain tumors in animals",
        category="artificial_color",
    ),
    IngredientInfo(
        key="green_3",
        name="Green 3 (Fast Green)",
        aliases=["fast green", "FD&C green 3", "E143", "green dye 3", "green #3"],
        severity=Severity.WARNING,
        reason="Bladder tumors in animals",
        category="artificial_color",
    ),
    IngredientInfo(
        key="citrus_red_2",
        name="Citrus Red 2",
        aliases=["E121", "citrus red no. 2"],
        severity=Severity.WARNING,
        reason="Potential carcinogen, only allowed on orange peels",
        category="artificial_color",
    ),
    IngredientInfo(
        key="orange_b",
        name="Orange B",
        aliases=["FD&C orange B"],
        severity=Severity.WARNING,
        reason="Limited use approved, potential carcinogen",
        category="artificial_color",
    ),
    # Other artificial sweeteners
    IngredientInfo(
        key="sucralose",
        name="Sucralose",
        aliases=["splenda", "E955"],
        severity=Severity.WARNING,
        reason="Gut microbiome disruption, insulin response",
        category="artificial_sweetener",
    ),
    IngredientInfo(
        key="acesulfame_k",
        name="Acesulfame-K",
        aliases=["acesulfame potassium", "ace-k", "E950", "acesulfame"],
        severity=Severity.WARNING,
        reason="Limited long-term safety data, contains methylene chloride",
        category="artificial_sweetener",
    ),
    IngredientInfo(
        key="saccharin",
        name="Saccharin",
        aliases=["sweet'n low", "E954"],
        severity=Severity.WARNING,
        reason="Historical cancer concerns",
        category="artificial_sweetener",
    ),
    # Preservatives (moderate concern)
    IngredientInfo(
        key="tbhq",
        name="TBHQ (Tertiary Butylhydroquinone)",
        aliases=["tertiary butylhydroquinone", "E319"],
        severity=Severity.WARNING,
        reason="Potential carcinogen at high doses",
        category="preservative",
    ),
    IngredientInfo(
        key="sodium_benzoate",
        name="Sodium Benzoate",
        aliases=["benzoate", "E211"],
        severity=Severity.WARNING,
        reason="Forms benzene with vitamin C, hyperactivity",
        category="preservative",
    ),
    IngredientInfo(
        key="potassium_sorbate",
        name="Potassium Sorbate",
        aliases=["sorbate", "E202"],
        severity=Severity.WARNING,
        reason="Potential DNA damage, allergic reactions",
        category="preservative",
    ),
    # Emulsifiers
    IngredientInfo(
        key="carrageenan",
        name="Carrageenan",
        aliases=["E407", "irish moss extract"],
        severity=Severity.WARNING,
        reason="Inflammation, gut issues",
        category="emulsifier",
    ),
    IngredientInfo(
        key="polysorbate_80",  # gitleaks:allow - additive identifier, not a secret
        name="Polysorbate 80",
        aliases=["polysorbate 60", "E433", "E432", "tween 80"],
        severity=Severity.WARNING,
        reason="Gut barrier disruption, inflammation",
        category="emulsifier",
    ),
    # Other additives
    IngredientInfo(
        key="azodicarbonamide",
        name="Azodicarbonamide",
        aliases=["E927", "ADA", "azo"],
        severity=Severity.WARNING,
        reason="Banned in EU and Australia, respiratory issues",
        category="dough_conditioner",
    ),
    IngredientInfo(
        key="titanium_dioxide",
        name="Titanium Dioxide",
        aliases=["E171", "TiO2"],
        severity=Severity.CRITICAL,
        reason="EFSA: genotoxicity cannot be ruled out, accumulates in body, banned in EU food",
        category="whitening_agent",
    ),
    IngredientInfo(
        key="diacetyl",
        name="Diacetyl",
        aliases=["butanedione"],
        severity=Severity.WARNING,
        reason="Respiratory issues (popcorn lung)",
        category="flavoring",
    ),
    # Aluminum compounds
    IngredientInfo(
        key="sodium_aluminum_phosphate",
        name="Sodium Aluminum Phosphate",
        aliases=["SALP", "E541", "aluminum phosphate"],
        severity=Severity.WARNING,
        reason="Aluminum accumulation concerns, neurotoxicity",
        category="leavening_agent",
    ),
    IngredientInfo(
        key="sodium_aluminum_sulfate",
        name="Sodium Aluminum Sulfate",
        aliases=["SAS", "aluminum sulfate"],
        severity=Severity.WARNING,
        reason="Aluminum accumulation, neurological concerns",
        category="leavening_agent",
    ),
    # Additional preservatives and additives
    IngredientInfo(
        key="calcium_disodium_edta",
        name="Calcium Disodium EDTA",
        aliases=["EDTA", "E385", "disodium EDTA", "edetic acid"],
        severity=Severity.WARNING,
        reason="May affect mineral absorption, synthetic chelating agent",
        category="preservative",
    ),
    IngredientInfo(
        key="calcium_propionate",
        name="Calcium Propionate",
        aliases=["E282", "propionate", "sodium propionate"],
        severity=Severity.WARNING,
        reason="Linked to behavioral issues in children, irritability",
        category="preservative",
    ),
    IngredientInfo(
        key="sodium_erythorbate",
        name="Sodium Erythorbate",
        aliases=["E316", "erythorbate"],
        severity=Severity.WARNING,
        reason="Synthetic preservative, may cause headaches",
        category="preservative",
    ),
    # Dough conditioners and emulsifiers
    IngredientInfo(
        key="datem",
        name="DATEM",
        aliases=[
            "diacetyl tartaric acid esters of monoglycerides",
            "E472e",
            "diacetyl tartaric acid ester",
        ],
        severity=Severity.WARNING,
        reason="Synthetic emulsifier, limited safety data",
        category="dough_conditioner",
    ),
    IngredientInfo(
        key="sodium_stearoyl_lactylate",
        name="Sodium Stearoyl Lactylate",
        aliases=["SSL", "E481", "calcium stearoyl lactylate", "E482"],
        severity=Severity.WARNING,
        reason="Synthetic emulsifier, may cause digestive issues",
        category="emulsifier",
    ),
    # Anti-foaming and processing aids
    IngredientInfo(
        key="dimethylpolysiloxane",
        name="Dimethylpolysiloxane",
        aliases=["E900", "PDMS", "silicone", "dimethicone"],
        severity=Severity.WARNING,
        reason="Industrial chemical used as anti-foaming agent",
        category="processing_aid",
    ),
    # Artificial flavors
    IngredientInfo(
        key="artificial_flavor",
        name="Artificial Flavor",
        aliases=["artificial flavors", "artificial flavoring", "artificially flavored"],
        severity=Severity.WARNING,
        reason="Synthetic chemicals, lack of transparency, potential allergens",
        category="flavoring",
    ),
    # Neotame (newer artificial sweetener)
    IngredientInfo(
        key="neotame",
        name="Neotame",
        aliases=["E961"],
        severity=Severity.WARNING,
        reason="Similar to aspartame but more potent, limited long-term data",
        category="artificial_sweetener",
    ),
    # Advantame
    IngredientInfo(
        key="advantame",
        name="Advantame",
        aliases=["E969"],
        severity=Severity.WARNING,
        reason="Derived from aspartame, extremely limited safety data",
        category="artificial_sweetener",
    ),
    # ==================== WATCH SEVERITY ====================
    IngredientInfo(
        key="natural_flavors",
        name="Natural Flavors",
        aliases=["natural flavor", "natural flavoring"],
        severity=Severity.WATCH,
        reason="Often contains MSG derivatives, lack of transparency",
        category="flavoring",
    ),
    IngredientInfo(
        key="caramel_color",
        name="Caramel Color",
        aliases=["E150c", "E150d", "caramel colour"],
        severity=Severity.WATCH,
        reason="Class III/IV may contain 4-MEI (carcinogen)",
        category="artificial_color",
    ),
    IngredientInfo(
        key="autolyzed_yeast",
        name="Autolyzed Yeast Extract",
        aliases=["autolyzed yeast", "yeast extract"],
        severity=Severity.WATCH,
        reason="Contains free glutamate (hidden MSG)",
        category="flavor_enhancer",
    ),
    IngredientInfo(
        key="propylene_glycol",
        name="Propylene Glycol",
        aliases=["E1520", "PG"],
        severity=Severity.WATCH,
        reason="Synthetic, industrial uses",
        category="solvent",
    ),
    IngredientInfo(
        key="sodium_phosphate",
        name="Sodium Phosphate",
        aliases=["phosphate", "E339", "trisodium phosphate", "disodium phosphate"],
        severity=Severity.WATCH,
        reason="Kidney concerns in excess",
        category="preservative",
    ),
    # Refined/processed ingredients
    IngredientInfo(
        key="maltodextrin",
        name="Maltodextrin",
        aliases=["corn maltodextrin", "rice maltodextrin"],
        severity=Severity.WATCH,
        reason="High glycemic index, blood sugar spikes",
        category="filler",
    ),
    IngredientInfo(
        key="corn_syrup",
        name="Corn Syrup",
        aliases=["glucose syrup", "corn syrup solids"],
        severity=Severity.WATCH,
        reason="Highly processed sugar, blood sugar impact",
        category="sweetener",
        exclude_patterns=["high fructose corn syrup", "hfcs"],
    ),
    IngredientInfo(
        key="modified_food_starch",
        name="Modified Food Starch",
        aliases=["modified corn starch", "modified starch", "modified tapioca starch"],
        severity=Severity.WATCH,
        reason="Chemically processed, low nutritional value",
        category="thickener",
    ),
    IngredientInfo(
        key="bleached_flour",
        name="Bleached Flour",
        aliases=["bleached wheat flour", "bleached enriched flour"],
        severity=Severity.WATCH,
        reason="Chemical bleaching agents, stripped nutrients",
        category="flour",
    ),
    # Thickeners and stabilizers
    IngredientInfo(
        key="cellulose",
        name="Cellulose",
        aliases=[
            "powdered cellulose",
            "microcrystalline cellulose",
            "cellulose gum",
            "cellulose gel",
            "E460",
            "E461",
            "E466",
        ],
        severity=Severity.WATCH,
        reason="Wood pulp filler, no nutritional value",
        category="filler",
    ),
    IngredientInfo(
        key="silicon_dioxide",
        name="Silicon Dioxide",
        aliases=["E551", "silica", "anti-caking agent"],
        severity=Severity.WATCH,
        reason="Industrial anti-caking agent, indigestible",
        category="anti_caking",
    ),
    IngredientInfo(
        key="magnesium_stearate",
        name="Magnesium Stearate",
        aliases=["E572", "stearic acid magnesium salt"],
        severity=Severity.WATCH,
        reason="Processing aid, may affect nutrient absorption",
        category="processing_aid",
    ),
    # Soy derivatives
    IngredientInfo(
        key="soy_lecithin",
        name="Soy Lecithin",
        aliases=["lecithin", "E322"],
        severity=Severity.WATCH,
        reason="Often from GMO soy, allergen for some",
        category="emulsifier",
    ),
    IngredientInfo(
        key="soy_protein_isolate",
        name="Soy Protein Isolate",
        aliases=["isolated soy protein", "soy protein concentrate"],
        severity=Severity.WATCH,
        reason="Highly processed, may contain hexane residues",
        category="protein",
    ),
    # Gums (can cause digestive issues for sensitive individuals)
    IngredientInfo(
        key="xanthan_gum",
        name="Xanthan Gum",
        aliases=["E415"],
        severity=Severity.WATCH,
        reason="May cause digestive issues in some people",
        category="thickener",
    ),
    IngredientInfo(
        key="guar_gum",
        name="Guar Gum",
        aliases=["E412"],
        severity=Severity.WATCH,
        reason="May cause bloating and gas in sensitive individuals",
        category="thickener",
    ),
    # Palm oil
    IngredientInfo(
        key="palm_oil",
        name="Palm Oil",
        aliases=["palm kernel oil", "palmitate", "palm fruit oil"],
        severity=Severity.WATCH,
        reason="High in saturated fat, environmental concerns",
        category="fat",
    ),
    # Dextrose and processed sugars
    IngredientInfo(
        key="dextrose",
        name="Dextrose",
        aliases=["glucose", "grape sugar"],
        severity=Severity.WATCH,
        reason="Refined sugar, rapid blood sugar spike",
        category="sweetener",
    ),
]


def _compile_ingredient_patterns() -> dict[str, re.Pattern]:
    """
    Pre-compile regex patterns for all ingredients.

    Uses word boundaries and negative lookbehind/lookahead to avoid
    false positives like "sugar free" matching "sugar".
    """
    patterns = {}

    for ingredient in BAD_INGREDIENTS:
        # Combine main name with aliases
        all_terms = [ingredient.name.lower()] + [a.lower() for a in ingredient.aliases]

        # Build pattern with word boundaries
        escaped_terms = [re.escape(term) for term in all_terms]
        base_pattern = r"\b(" + "|".join(escaped_terms) + r")\b"

        # Note: exclude_patterns are checked separately in check_product_safety()
        # rather than being built into the regex pattern

        patterns[ingredient.key] = re.compile(base_pattern, re.IGNORECASE)

    return patterns


# Pre-compiled patterns for performance
_INGREDIENT_PATTERNS = _compile_ingredient_patterns()


@dataclass
class IngredientMatch:
    """A single matched bad ingredient."""

    ingredient_key: str
    ingredient_name: str
    severity: Severity
    reason: str
    category: str
    matched_text: str


@dataclass
class PositiveAttribute:
    """A food quality attribute that positively scores a product."""

    key: str
    name: str
    aliases: list[str]
    bonus: int
    benefit: str
    category: str


@dataclass
class AttributeMatch:
    """A matched positive food quality attribute."""

    attribute_key: str
    attribute_name: str
    bonus: int
    benefit: str
    matched_text: str


# Positive food quality attributes that improve safety score
GOOD_ATTRIBUTES: list[PositiveAttribute] = [
    PositiveAttribute(
        key="organic",
        name="Organic",
        aliases=["usda organic", "certified organic", "100% organic"],
        bonus=0,
        benefit="No synthetic pesticides, herbicides, or GMOs",
        category="certification",
    ),
    PositiveAttribute(
        key="non_gmo",
        name="Non-GMO",
        aliases=["non gmo", "non-gmo verified", "no gmo", "gmo free", "gmo-free"],
        bonus=0,
        benefit="No genetically modified organisms",
        category="certification",
    ),
    PositiveAttribute(
        key="grass_fed",
        name="Grass-Fed / Pasture-Raised / Wild-Caught",
        aliases=[
            "grass fed",
            "grassfed",
            "grass-fed",
            "pasture raised",
            "pasture-raised",
            "wild caught",
            "wild-caught",
        ],
        bonus=15,
        benefit="Better nutrient profile, higher omega-3s",
        category="animal_welfare",
    ),
    PositiveAttribute(
        key="free_range",
        name="Free-Range",
        aliases=["free range", "free-range", "cage free", "cage-free"],
        bonus=10,
        benefit="Better living conditions, improved nutrition",
        category="animal_welfare",
    ),
    PositiveAttribute(
        key="hormone_free",
        name="Hormone-Free / Antibiotic-Free",
        aliases=[
            "no hormones",
            "hormone free",
            "hormone-free",
            "no antibiotics",
            "antibiotic free",
            "antibiotic-free",
            "raised without antibiotics",
        ],
        bonus=10,
        benefit="No synthetic growth hormones or antibiotics",
        category="production",
    ),
    PositiveAttribute(
        key="all_natural",
        name="All Natural",
        aliases=["all natural", "all-natural", "100% natural"],
        bonus=8,
        benefit="No artificial ingredients or synthetic preservatives",
        category="quality",
    ),
    PositiveAttribute(
        key="no_artificial",
        name="No Artificial Ingredients",
        aliases=[
            "no artificial",
            "no artificial ingredients",
            "no artificial colors",
            "no artificial flavors",
            "no artificial preservatives",
        ],
        bonus=8,
        benefit="Free of artificial colors, flavors, and preservatives",
        category="quality",
    ),
    PositiveAttribute(
        key="whole_grain",
        name="Whole Grain",
        aliases=[
            "whole grain",
            "whole grains",
            "whole wheat",
            "100% whole wheat",
            "100% whole grain",
        ],
        bonus=15,
        benefit="Higher fiber, better blood sugar response",
        category="quality",
    ),
    PositiveAttribute(
        key="whole_food",
        name="Whole Food",
        aliases=[
            "100% juice",
            "single ingredient",
            "nothing artificial",
            "no added ingredients",
            "just fruit",
            "just vegetables",
            "simple ingredients",
            "minimally processed",
        ],
        bonus=12,
        benefit="Whole, minimally processed food with simple ingredients",
        category="quality",
    ),
    # --- Whole food ingredient recognition ---
    # These match bare ingredient names (e.g. "olive oil", "chicken breast")
    # so that per-ingredient scoring in recipes produces meaningful grades
    # instead of everything defaulting to base-60 / C.
    PositiveAttribute(
        key="healthy_fat_ingredient",
        name="Healthy Fat",
        aliases=[
            "olive oil",
            "extra virgin olive oil",
            "avocado oil",
            "avocado",
            "almond",
            "almonds",
            "walnut",
            "walnuts",
            "cashew",
            "cashews",
            "flaxseed",
            "chia",
            "hemp seed",
            "coconut oil",
            "sesame oil",
            "peanut butter",
        ],
        bonus=20,
        benefit="Heart-healthy fat source",
        category="whole_food",
    ),
    PositiveAttribute(
        key="produce_ingredient",
        name="Fresh Produce",
        aliases=[
            "spinach",
            "kale",
            "broccoli",
            "carrot",
            "carrots",
            "tomato",
            "tomatoes",
            "onion",
            "onions",
            "garlic",
            "pepper",
            "peppers",
            "bell pepper",
            "lettuce",
            "cucumber",
            "zucchini",
            "asparagus",
            "celery",
            "cauliflower",
            "sweet potato",
            "sweet potatoes",
            "potato",
            "potatoes",
            "mushroom",
            "mushrooms",
            "green beans",
            "peas",
            "corn",
            "cabbage",
            "eggplant",
            "beets",
            "radish",
            "brussels sprouts",
            "artichoke",
        ],
        bonus=20,
        benefit="Nutrient-dense whole vegetable",
        category="whole_food",
    ),
    PositiveAttribute(
        key="fruit_ingredient",
        name="Fresh Fruit",
        aliases=[
            "apple",
            "apples",
            "banana",
            "bananas",
            "berry",
            "berries",
            "strawberry",
            "strawberries",
            "blueberry",
            "blueberries",
            "raspberry",
            "raspberries",
            "blackberry",
            "blackberries",
            "lemon",
            "lemons",
            "lime",
            "limes",
            "orange",
            "oranges",
            "peach",
            "peaches",
            "pear",
            "pears",
            "mango",
            "mangoes",
            "pineapple",
            "grape",
            "grapes",
            "watermelon",
            "cantaloupe",
            "cherry",
            "cherries",
            "plum",
            "plums",
            "fig",
            "figs",
            "pomegranate",
            "kiwi",
            "grapefruit",
            "cranberry",
            "cranberries",
        ],
        bonus=20,
        benefit="Nutrient-dense whole fruit",
        category="whole_food",
    ),
    PositiveAttribute(
        key="lean_protein_ingredient",
        name="Lean Protein",
        aliases=[
            "chicken",
            "chicken breast",
            "chicken thigh",
            "turkey",
            "ground turkey",
            "salmon",
            "tuna",
            "shrimp",
            "cod",
            "tilapia",
            "halibut",
            "trout",
            "egg",
            "eggs",
            "lentil",
            "lentils",
            "chickpea",
            "chickpeas",
            "black bean",
            "black beans",
            "kidney bean",
            "kidney beans",
            "tofu",
            "tempeh",
            "pork tenderloin",
            "pork loin",
        ],
        bonus=20,
        benefit="Quality protein source",
        category="whole_food",
    ),
    PositiveAttribute(
        key="whole_grain_ingredient",
        name="Whole Grain",
        aliases=[
            "brown rice",
            "quinoa",
            "oats",
            "oatmeal",
            "whole wheat",
            "farro",
            "barley",
            "bulgur",
            "millet",
            "buckwheat",
            "wild rice",
        ],
        bonus=20,
        benefit="Whole grain with fiber and nutrients",
        category="whole_food",
    ),
    PositiveAttribute(
        key="herb_spice_ingredient",
        name="Herb or Spice",
        aliases=[
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
            "chili powder",
            "cayenne",
            "coriander",
            "nutmeg",
            "cloves",
            "bay leaf",
            "bay leaves",
            "sage",
            "tarragon",
            "chives",
            "fennel",
            "cardamom",
            "saffron",
        ],
        bonus=20,
        benefit="Natural seasoning with health benefits",
        category="whole_food",
    ),
    PositiveAttribute(
        key="dairy_ingredient",
        name="Natural Dairy",
        aliases=[
            "milk",
            "butter",
            "cream",
            "heavy cream",
            "yogurt",
            "greek yogurt",
            "sour cream",
            "parmesan",
            "mozzarella",
            "cheddar",
            "cheese",
            "cream cheese",
            "ricotta",
            "feta",
            "gouda",
        ],
        bonus=15,
        benefit="Natural dairy product",
        category="whole_food",
    ),
    PositiveAttribute(
        key="pantry_staple_ingredient",
        name="Pantry Staple",
        aliases=[
            "flour",
            "sugar",
            "salt",
            "honey",
            "maple syrup",
            "vinegar",
            "soy sauce",
            "rice",
            "pasta",
            "bread",
            "tortilla",
            "stock",
            "broth",
            "tomato paste",
            "tomato sauce",
            "coconut milk",
        ],
        bonus=10,
        benefit="Basic cooking staple",
        category="whole_food",
    ),
]

# Pre-compile positive attribute patterns
_POSITIVE_PATTERNS: list[dict] = []


def _compile_positive_patterns() -> list[dict]:
    compiled = []
    for attr in GOOD_ATTRIBUTES:
        all_terms = [attr.name.lower()] + [a.lower() for a in attr.aliases]
        for term in all_terms:
            compiled.append(
                {
                    "pattern": re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE),
                    "key": attr.key,
                    "name": attr.name,
                    "bonus": attr.bonus,
                    "benefit": attr.benefit,
                    "term": term,
                }
            )
    return compiled


_POSITIVE_PATTERNS = _compile_positive_patterns()


# Pantry-staple seasonings worth treating as spices for cart gating.
# Standalone "pepper" is intentionally excluded — it collides with "bell pepper",
# "red pepper", and other produce. Only multi-word forms that unambiguously name
# the spice are included.
_SPICE_EXTRA_ALIASES: frozenset[str] = frozenset(
    {
        "salt",
        "black pepper",
        "white pepper",
        "ground pepper",
        "peppercorn",
        "peppercorns",
        "red pepper flakes",
        "crushed red pepper",
        "soy sauce",
        "vinegar",
    }
)


def _build_spice_pattern() -> re.Pattern:
    terms: set[str] = set()
    for attr in GOOD_ATTRIBUTES:
        if attr.key == "herb_spice_ingredient":
            terms.update(a.lower() for a in attr.aliases)
    terms.update(_SPICE_EXTRA_ALIASES)
    # Longest-first ordering ensures multi-word phrases win over substrings.
    ordered = sorted(terms, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b",
        re.IGNORECASE,
    )


_SPICE_PATTERN: re.Pattern = _build_spice_pattern()


def is_spice(name: str | None) -> bool:
    """Return True when `name` reads as a herb, spice, or seasoning.

    Backed by the alias list already declared in GOOD_ATTRIBUTES so the source
    of truth stays single. Parenthetical notes (e.g. "(optional)") are stripped
    before matching. Word boundaries prevent false positives like "salted
    butter" matching "salt".
    """
    if not name:
        return False
    cleaned = re.sub(r"\([^)]*\)", " ", name).lower()
    return bool(_SPICE_PATTERN.search(cleaned))


def check_positive_attributes(text: str) -> list[AttributeMatch]:
    """Check a product description for positive food quality attributes."""
    text_lower = text.lower()
    matched: list[AttributeMatch] = []
    seen_keys: set = set()

    for entry in _POSITIVE_PATTERNS:
        if entry["key"] in seen_keys:
            continue
        if entry["pattern"].search(text_lower):
            matched.append(
                AttributeMatch(
                    attribute_key=entry["key"],
                    attribute_name=entry["name"],
                    bonus=entry["bonus"],
                    benefit=entry["benefit"],
                    matched_text=entry["term"],
                )
            )
            seen_keys.add(entry["key"])

    return matched


def score_product(
    positive_matches: list[AttributeMatch],
    negative_matches: list[IngredientMatch],
) -> tuple:
    """Compute safety score (0-100) and grade (A-F) for a product."""
    score = 60  # base score

    # Positive bonuses, capped at +35
    bonus = min(35, sum(a.bonus for a in positive_matches))
    score += bonus

    # Negative deductions
    for m in negative_matches:
        if m.severity == Severity.CRITICAL:
            score -= 30
        elif m.severity == Severity.WARNING:
            score -= 20
        else:  # WATCH
            score -= 8

    score = max(0, min(100, score))

    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 45:
        grade = "D"
    else:
        grade = "F"

    return score, grade


def score_to_status(score: int) -> str:
    """Convert a numeric safety score to a status label."""
    if score >= 90:
        return "excellent"
    elif score >= 75:
        return "good"
    elif score >= 60:
        return "acceptable"
    elif score >= 45:
        return "poor"
    else:
        return "avoid"


@dataclass
class SafetyResult:
    """Result of checking a product for bad ingredients."""

    has_concerns: bool
    highest_severity: Severity | None
    matches: list[IngredientMatch]
    positive_attributes: list[AttributeMatch] = field(default_factory=list)
    score: int = 60
    grade: str = "C"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "has_concerns": self.has_concerns,
            "highest_severity": self.highest_severity.value if self.highest_severity else None,
            "score": self.score,
            "grade": self.grade,
            "flagged_ingredients": [
                {
                    "ingredient": m.ingredient_name,
                    "severity": m.severity.value,
                    "reason": m.reason,
                    "category": m.category,
                    "matched_text": m.matched_text,
                }
                for m in self.matches
            ],
            "positive_attributes": [
                {
                    "attribute": a.attribute_name,
                    "bonus": a.bonus,
                    "benefit": a.benefit,
                    "matched_text": a.matched_text,
                }
                for a in self.positive_attributes
            ],
        }


def check_product_safety(
    description: str,
    brand: str | None = None,
    categories: list[str] | None = None,
    disabled_ingredients: set | None = None,
    force_refresh_patterns: bool = False,
) -> SafetyResult:
    """
    Check a product for bad ingredients based on its description.

    Now uses get_compiled_patterns() which includes custom ingredients.

    Args:
        description: Product description/name to scan
        brand: Brand name (not scanned to avoid false positives)
        categories: Product categories (for context, not currently used)
        disabled_ingredients: Set of ingredient keys to skip
        force_refresh_patterns: Force pattern cache refresh (used after ingredient changes)

    Returns:
        SafetyResult with all matched ingredients
    """
    if not description:
        return SafetyResult(
            has_concerns=False,
            highest_severity=None,
            matches=[],
            positive_attributes=[],
            score=60,
            grade="C",
        )

    text = description.lower()
    matches: list[IngredientMatch] = []
    disabled = disabled_ingredients or set()

    # Get patterns (cached, includes custom ingredients)
    pattern_data = get_compiled_patterns(force_refresh=force_refresh_patterns)
    patterns = pattern_data["patterns"]

    # Check for exclusions from hardcoded ingredients
    exclusions_map = {}
    for ingredient in BAD_INGREDIENTS:
        if ingredient.exclude_patterns:
            exclusions_map[ingredient.key] = ingredient.exclude_patterns

    for pattern_info in patterns:
        # Skip if user disabled this ingredient check
        if pattern_info["key"] in disabled:
            continue

        pattern = pattern_info["pattern"]
        match = pattern.search(text)
        if match:
            # Check exclusion patterns (from hardcoded ingredients)
            if pattern_info["key"] in exclusions_map:
                skip = False
                for excl in exclusions_map[pattern_info["key"]]:
                    if excl.lower() in text:
                        skip = True
                        break
                if skip:
                    continue

            # Map severity string to Severity enum
            severity_map = {
                "critical": Severity.CRITICAL,
                "warning": Severity.WARNING,
                "watch": Severity.WATCH,
            }
            severity = severity_map.get(pattern_info["severity"], Severity.WATCH)

            matches.append(
                IngredientMatch(
                    ingredient_key=pattern_info["key"],
                    ingredient_name=pattern_info["name"],
                    severity=severity,
                    reason=pattern_info["reason"],
                    category=pattern_info["category"],
                    matched_text=match.group(0),
                )
            )

    # Determine highest severity
    highest_severity = None
    if matches:
        severity_order = [Severity.CRITICAL, Severity.WARNING, Severity.WATCH]
        for sev in severity_order:
            if any(m.severity == sev for m in matches):
                highest_severity = sev
                break

    positive_attributes = check_positive_attributes(description)
    score, grade = score_product(positive_attributes, matches)

    return SafetyResult(
        has_concerns=len(matches) > 0,
        highest_severity=highest_severity,
        matches=matches,
        positive_attributes=positive_attributes,
        score=score,
        grade=grade,
    )


def get_ingredient_by_key(key: str) -> IngredientInfo | None:
    """Get ingredient info by its key."""
    for ing in BAD_INGREDIENTS:
        if ing.key == key:
            return ing
    return None


def get_all_ingredients() -> list[dict[str, Any]]:
    """Get all bad ingredients as a list of dictionaries."""
    return [
        {
            "key": ing.key,
            "name": ing.name,
            "aliases": ing.aliases,
            "severity": ing.severity.value,
            "reason": ing.reason,
            "category": ing.category,
        }
        for ing in BAD_INGREDIENTS
    ]


def get_ingredients_by_severity(severity: Severity) -> list[dict[str, Any]]:
    """Get ingredients filtered by severity level."""
    return [
        {
            "key": ing.key,
            "name": ing.name,
            "aliases": ing.aliases,
            "severity": ing.severity.value,
            "reason": ing.reason,
            "category": ing.category,
        }
        for ing in BAD_INGREDIENTS
        if ing.severity == severity
    ]


def get_ingredients_by_category(category: str) -> list[dict[str, Any]]:
    """Get ingredients filtered by category."""
    return [
        {
            "key": ing.key,
            "name": ing.name,
            "aliases": ing.aliases,
            "severity": ing.severity.value,
            "reason": ing.reason,
            "category": ing.category,
        }
        for ing in BAD_INGREDIENTS
        if ing.category == category
    ]


def get_categories() -> list[str]:
    """Get all unique ingredient categories."""
    return sorted(set(ing.category for ing in BAD_INGREDIENTS))


# ==================== DYNAMIC INGREDIENT MANAGEMENT ====================

# Module-level cache for compiled patterns
_pattern_cache = None
_pattern_cache_timestamp = None
_CACHE_TTL = 300  # 5 minutes


def get_active_ingredients(include_custom: bool = True) -> list[dict[str, Any]]:
    """
    Get all active ingredients from hardcoded + custom + overrides.

    Algorithm:
    1. Start with BAD_INGREDIENTS (system defaults)
    2. Apply ingredient_overrides (severity changes, hiding)
    3. Add custom_ingredients where is_active=1
    4. Filter out hidden ingredients
    5. Merge duplicate entries (custom can override system)

    Returns unified list with all active ingredients.
    """
    import json

    from kroger_mcp.analytics.database import get_db_connection

    # Start with system defaults
    ingredients = []
    for ing in BAD_INGREDIENTS:
        ingredients.append(
            {
                "name": ing.name,
                "severity": ing.severity.value,
                "category": ing.category,
                "reason": ing.reason,
                "aliases": list(ing.aliases),
                "source": "system",
                "key": ing.key,
            }
        )

    # Apply overrides to system ingredients
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT ingredient_name, override_severity, override_reason,
                   additional_aliases, is_hidden
            FROM ingredient_overrides
        """
        )
        overrides = {row["ingredient_name"].lower(): row for row in cursor.fetchall()}

        # Filter out hidden ingredients and apply overrides
        filtered_ingredients = []
        for ing in ingredients:
            override = overrides.get(ing["name"].lower())
            if override and override["is_hidden"]:
                continue  # Skip hidden ingredients

            if override:
                if override["override_severity"]:
                    ing["severity"] = override["override_severity"]
                if override["override_reason"]:
                    ing["reason"] = override["override_reason"]
                if override["additional_aliases"]:
                    try:
                        extra = json.loads(override["additional_aliases"])
                        ing["aliases"].extend(extra)
                    except (json.JSONDecodeError, TypeError):
                        pass

            filtered_ingredients.append(ing)

        ingredients = filtered_ingredients

        # Add custom ingredients if requested
        if include_custom:
            cursor = conn.execute(
                """
                SELECT ingredient_name, severity, category, reason, aliases
                FROM custom_ingredients
                WHERE is_active = 1
            """
            )

            for row in cursor.fetchall():
                aliases = []
                if row["aliases"]:
                    try:
                        aliases = json.loads(row["aliases"])
                    except (json.JSONDecodeError, TypeError):
                        aliases = []

                ingredients.append(
                    {
                        "name": row["ingredient_name"],
                        "severity": row["severity"],
                        "category": row["category"] or "",
                        "reason": row["reason"] or "",
                        "aliases": aliases,
                        "source": "custom",
                        "key": f"custom_{row['ingredient_name'].lower().replace(' ', '_')}",
                    }
                )

    finally:
        conn.close()

    return ingredients


def get_compiled_patterns(force_refresh: bool = False) -> dict[str, Any]:
    """
    Get compiled regex patterns with caching.

    Patterns are cached for 5 minutes. Use force_refresh=True to rebuild
    immediately after ingredient changes.
    """
    global _pattern_cache, _pattern_cache_timestamp

    # Check cache validity
    if not force_refresh and _pattern_cache is not None:
        from datetime import datetime

        if _pattern_cache_timestamp:
            cache_age = (datetime.now() - _pattern_cache_timestamp).total_seconds()
            if cache_age < _CACHE_TTL:
                return _pattern_cache

    # Rebuild patterns
    from datetime import datetime

    ingredients = get_active_ingredients()
    patterns = []

    for ing in ingredients:
        all_names = [ing["name"]] + ing["aliases"]
        for name in all_names:
            escaped = re.escape(name)
            pattern = re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
            patterns.append(
                {
                    "pattern": pattern,
                    "severity": ing["severity"],
                    "name": ing["name"],
                    "reason": ing["reason"],
                    "category": ing["category"],
                    "key": ing.get("key", ing["name"].lower().replace(" ", "_")),
                }
            )

    _pattern_cache = {
        "patterns": patterns,
        "timestamp": datetime.now().isoformat(),
        "ingredient_count": len(ingredients),
    }
    _pattern_cache_timestamp = datetime.now()

    return _pattern_cache


# ==================== ASYNC WRAPPERS ====================
# These allow async tool handlers to call blocking DB functions without
# blocking the event loop. Use these from async tool handlers instead of
# the sync versions.


async def get_active_ingredients_async(include_custom: bool = True) -> list[dict[str, Any]]:
    """Async wrapper for get_active_ingredients() — runs in thread pool."""
    return await asyncio.to_thread(get_active_ingredients, include_custom)


async def get_compiled_patterns_async(force_refresh: bool = False) -> dict[str, Any]:
    """Async wrapper for get_compiled_patterns() — runs in thread pool."""
    return await asyncio.to_thread(get_compiled_patterns, force_refresh)
