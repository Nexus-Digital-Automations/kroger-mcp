"""Performance-refactor regression tests (Phase 2).

Covers two optimizations that must not change observable behavior:

1. ``meal_planning._get_recipe_from_json`` now serves lookups from a
   process-local {id: recipe} index built by reading the recipes JSON *once*,
   instead of re-reading + linearly scanning the file on every call. These
   tests assert correct lookups, the None-on-missing contract, and that the
   file is read at most once across many lookups.

2. ``recipe_scoring.calculate_health_score`` hoists its keyword collections to
   module-level frozensets (built once) instead of rebuilding ``all_keywords``
   per call. The matching semantics must be byte-for-byte identical. We assert
   equivalence against an independent reference implementation of the original
   list-based matching on representative ingredient lists.
"""

from __future__ import annotations

import json
from typing import Any

import kroger_mcp.analytics.meal_planning as mp
import kroger_mcp.analytics.recipe_scoring as rs

# ---------------------------------------------------------------------------
# Task 2A — recipe load-once index
# ---------------------------------------------------------------------------


def _write_recipes(tmp_path, recipes: list[dict[str, Any]]) -> str:
    path = tmp_path / "kroger_recipes.json"
    path.write_text(json.dumps({"recipes": recipes}))
    return str(path)


def test_get_recipe_returns_right_recipe_and_none_for_missing(tmp_path, monkeypatch):
    recipes = [
        {"id": "r1", "name": "Soup", "ingredients": []},
        {"id": "r2", "name": "Salad", "ingredients": []},
    ]
    path = _write_recipes(tmp_path, recipes)

    monkeypatch.setattr(mp, "RECIPES_FILE", path)
    mp._invalidate_recipes_cache()

    got = mp._get_recipe_from_json("r2")
    assert got is not None
    assert got["name"] == "Salad"
    # Exact return shape preserved: the whole recipe dict.
    assert got == recipes[1]

    # None-on-missing contract.
    assert mp._get_recipe_from_json("does-not-exist") is None

    mp._invalidate_recipes_cache()


def test_get_recipe_reads_file_once_across_many_lookups(tmp_path, monkeypatch):
    recipes = [{"id": f"r{i}", "name": f"Recipe {i}", "ingredients": []} for i in range(50)]
    path = _write_recipes(tmp_path, recipes)
    monkeypatch.setattr(mp, "RECIPES_FILE", path)
    mp._invalidate_recipes_cache()

    # Count real file opens of the recipes JSON. The build path uses the
    # module-level builtin `open`, so wrap it and count opens of our file.
    open_calls = {"n": 0}
    real_open = open

    def counting_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if file == path:
            open_calls["n"] += 1
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    # Many lookups (hits and a miss) — the file must be read exactly once.
    for i in range(50):
        assert mp._get_recipe_from_json(f"r{i}") is not None
    assert mp._get_recipe_from_json("missing") is None
    assert mp._get_recipe_from_json("r0") is not None

    assert open_calls["n"] == 1, f"expected 1 file read, got {open_calls['n']}"

    mp._invalidate_recipes_cache()


def test_index_rebuilds_when_file_changes(tmp_path, monkeypatch):
    """A rewrite (new mtime/size) must be picked up via the fingerprint."""
    path = _write_recipes(tmp_path, [{"id": "r1", "name": "Old", "ingredients": []}])
    monkeypatch.setattr(mp, "RECIPES_FILE", path)
    mp._invalidate_recipes_cache()

    assert mp._get_recipe_from_json("r1")["name"] == "Old"

    # Rewrite with a different recipe set; bump mtime explicitly so the change
    # is detected even on coarse-resolution filesystems.
    import os

    p = tmp_path / "kroger_recipes.json"
    p.write_text(json.dumps({"recipes": [{"id": "r1", "name": "New", "ingredients": []}]}))
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))

    assert mp._get_recipe_from_json("r1")["name"] == "New"

    mp._invalidate_recipes_cache()


# ---------------------------------------------------------------------------
# Task 2D — score-preserving keyword matching
# ---------------------------------------------------------------------------
#
# Independent reference implementation of the ORIGINAL (pre-refactor) matching,
# using plain Python lists and the same substring tests. If the optimized code
# is score-preserving, the bonus/penalty components computed here must equal the
# components implied by `calculate_health_score`'s output.


def _reference_components(ing_names: list[str]) -> dict[str, int]:
    ing_names_lower = [n.lower() for n in ing_names]
    all_ing_text = " ".join(ing_names_lower)
    total = len(ing_names_lower)

    # ORIGINAL: plain lists, rebuilt all_keywords.
    categories_detected: list[str] = []
    for cat, keywords in rs.HEALTHY_CATEGORIES.items():
        if any(kw in all_ing_text for kw in keywords):
            categories_detected.append(cat)
    cat_score = min(len(categories_detected) * 7, 35)

    all_keywords = [kw for kws in rs.HEALTHY_CATEGORIES.values() for kw in kws]
    non_staple = [
        name
        for name in ing_names_lower
        if not any(staple in name for staple in rs.NEUTRAL_STAPLES)
    ]
    denom = max(1, len(non_staple))
    quality_hits = sum(1 for name in non_staple if any(kw in name for kw in all_keywords))
    quality_score = round((quality_hits / denom) * 30)

    whole_hits = sum(
        1 for name in ing_names_lower if any(sig in name for sig in rs.WHOLE_FOOD_SIGNALS)
    )
    whole_score = min(round((whole_hits / total) * 15), 5) if total else 0

    proc_penalty = 0
    for name in ing_names_lower:
        scan = name.replace("instant pot", "")
        if any(ind in scan for ind in rs.PROCESSED_INDICATORS):
            proc_penalty += 5
    proc_penalty = min(proc_penalty, 15)

    conv_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in rs.CONVENIENCE_INDICATORS):
            conv_penalty += 3
    conv_penalty = min(conv_penalty, 8)

    heavy_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in rs.HEAVY_NEGATIVES):
            heavy_penalty += 3
    heavy_penalty = min(heavy_penalty, 10)

    sugar_penalty = 0
    for name in ing_names_lower:
        if "stevia" in name:
            continue
        if any(kw in name for kw in rs.SUGAR_KEYWORDS):
            sugar_penalty += 2
    sugar_penalty = min(sugar_penalty, 6)

    return {
        "categories": len(categories_detected),
        "cat_score": cat_score,
        "quality_score": quality_score,
        "whole_score": whole_score,
        "proc_penalty": proc_penalty,
        "conv_penalty": conv_penalty,
        "heavy_penalty": heavy_penalty,
        "sugar_penalty": sugar_penalty,
    }


def _optimized_components(ing_names: list[str]) -> dict[str, int]:
    """Same computation using the optimized module-level frozensets."""
    ing_names_lower = [n.lower() for n in ing_names]
    all_ing_text = " ".join(ing_names_lower)
    total = len(ing_names_lower)

    categories_detected: list[str] = []
    for cat, keywords in rs.HEALTHY_CATEGORY_KEYWORDS.items():
        if any(kw in all_ing_text for kw in keywords):
            categories_detected.append(cat)
    cat_score = min(len(categories_detected) * 7, 35)

    non_staple = [
        name
        for name in ing_names_lower
        if not any(staple in name for staple in rs.NEUTRAL_STAPLE_SET)
    ]
    denom = max(1, len(non_staple))
    quality_hits = sum(
        1 for name in non_staple if any(kw in name for kw in rs.ALL_HEALTHY_KEYWORDS)
    )
    quality_score = round((quality_hits / denom) * 30)

    whole_hits = sum(
        1 for name in ing_names_lower if any(sig in name for sig in rs.WHOLE_FOOD_SIGNAL_SET)
    )
    whole_score = min(round((whole_hits / total) * 15), 5) if total else 0

    proc_penalty = 0
    for name in ing_names_lower:
        scan = name.replace("instant pot", "")
        if any(ind in scan for ind in rs.PROCESSED_INDICATOR_SET):
            proc_penalty += 5
    proc_penalty = min(proc_penalty, 15)

    conv_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in rs.CONVENIENCE_INDICATOR_SET):
            conv_penalty += 3
    conv_penalty = min(conv_penalty, 8)

    heavy_penalty = 0
    for name in ing_names_lower:
        if any(ind in name for ind in rs.HEAVY_NEGATIVE_SET):
            heavy_penalty += 3
    heavy_penalty = min(heavy_penalty, 10)

    sugar_penalty = 0
    for name in ing_names_lower:
        if "stevia" in name:
            continue
        if any(kw in name for kw in rs.SUGAR_KEYWORD_SET):
            sugar_penalty += 2
    sugar_penalty = min(sugar_penalty, 6)

    return {
        "categories": len(categories_detected),
        "cat_score": cat_score,
        "quality_score": quality_score,
        "whole_score": whole_score,
        "proc_penalty": proc_penalty,
        "conv_penalty": conv_penalty,
        "heavy_penalty": heavy_penalty,
        "sugar_penalty": sugar_penalty,
    }


# Representative ingredient lists exercising every match branch.
_SAMPLE_INGREDIENT_LISTS = [
    # Clean whole-food recipe.
    ["fresh broccoli", "olive oil", "garlic", "lemon juice", "quinoa", "salmon"],
    # Plurals/compounds that only match via substring.
    ["green beans", "blueberries", "cranberries", "sweet potato", "bok choy"],
    # Processed + convenience + heavy + sugar penalties.
    ["cream of mushroom soup", "canned beans", "bacon", "brown sugar", "rotisserie chicken"],
    # "instant pot" must NOT trigger the "instant" processed penalty.
    ["instant pot beef", "carrots", "onion"],
    # Stevia exclusion from sugar; staples excluded from quality denom.
    ["stevia", "salt", "black pepper", "water", "spinach"],
    # Multi-word whole-food signals.
    ["grass-fed beef", "wild-caught cod", "skin-on chicken thighs"],
    # Empty-ish edge (single staple).
    ["salt"],
]


def test_score_components_match_reference():
    for names in _SAMPLE_INGREDIENT_LISTS:
        ref = _reference_components(names)
        opt = _optimized_components(names)
        assert opt == ref, f"component mismatch for {names}: {opt} != {ref}"


def test_calculate_health_score_matches_reference_total(monkeypatch):
    """End-to-end: score == base(20) + bonus - penalties from the reference.

    Uses name-only ingredients (no product_id) so no DB/USDA path runs and the
    BAD_INGREDIENTS penalty equals the safety-scan over the names themselves.
    Redis is disabled so a live local instance can't serve cached scores.
    """
    import kroger_mcp.cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_redis", lambda: None)
    from kroger_mcp.analytics.ingredients import check_product_safety

    _PENALTY_CAPS = {"critical": 45, "warning": 24, "watch": 12}
    _PENALTY_PER_MATCH = {"critical": 15, "warning": 8, "watch": 3}

    for names in _SAMPLE_INGREDIENT_LISTS:
        recipe = {"ingredients": [{"name": n} for n in names]}
        result = rs.calculate_health_score(recipe, names_only=True)

        comp = _reference_components(names)

        # Reproduce the BAD_INGREDIENTS penalty over the same name-only scans.
        severity_counts = {"critical": 0, "warning": 0, "watch": 0}
        for n in names:
            for match in check_product_safety(n).matches:
                severity_counts[match.severity.value] += 1
        bad_ing_penalty = sum(
            min(c * _PENALTY_PER_MATCH[s], _PENALTY_CAPS[s])
            for s, c in severity_counts.items()
        )

        base = 20
        bonus = comp["cat_score"] + comp["quality_score"] + comp["whole_score"]
        total_penalty = (
            comp["proc_penalty"]
            + comp["conv_penalty"]
            + comp["heavy_penalty"]
            + comp["sugar_penalty"]
            + bad_ing_penalty
        )
        expected = max(0, min(100, base + bonus - total_penalty))

        assert result["score"] == expected, (
            f"score mismatch for {names}: got {result['score']}, expected {expected}"
        )
        assert result["bonus_applied"] == bonus
        assert len(result["categories_detected"]) == comp["categories"]
