#!/usr/bin/env python3
"""Verify the recipe->guide migration result.

Owner: acceptance check for specs/move-guides-out-of-recipes.md.

Asserts, against a recipes/guides JSON pair, that each target name is gone from
recipes, present exactly once in guides, and that each migrated guide is well
formed (non-empty description + steps, list tags, and an ingredients section).
Exit 0 = all good; exit 1 = a check failed (prints the failures).

Usage:
    python3 verify_guide_migration.py [RECIPES_JSON] [GUIDES_JSON]
"""

from __future__ import annotations

import json
import sys

TARGET_NAMES = [
    "Master Guide: Cooking Dry Beans with Soaking",
    "Bean Cooking Starter Kit",
    "Quick-Cooking Lentils & Split Peas (No Soak)",
]
_ING_MARKER = "— INGREDIENTS / SHOPPING LIST —"


def verify(recipes_path: str, guides_path: str) -> list[str]:
    failures: list[str] = []

    with open(recipes_path, encoding="utf-8") as fh:
        recipes_doc = json.load(fh)
    with open(guides_path, encoding="utf-8") as fh:
        guides_doc = json.load(fh)

    if not isinstance(recipes_doc.get("recipes"), list):
        failures.append("recipes doc missing a 'recipes' list")
    if not isinstance(guides_doc.get("guides"), list):
        failures.append("guides doc missing a 'guides' list")
    if failures:
        return failures

    recipe_names = [r.get("name") for r in recipes_doc["recipes"]]
    guides_by_name: dict[str, list[dict]] = {}
    for g in guides_doc["guides"]:
        guides_by_name.setdefault(g.get("name"), []).append(g)

    for name in TARGET_NAMES:
        if name in recipe_names:
            failures.append(f"target still present in recipes: {name}")

        matches = guides_by_name.get(name, [])
        if len(matches) != 1:
            failures.append(f"expected exactly 1 guide named {name!r}, found {len(matches)}")
            continue

        guide = matches[0]
        if not (guide.get("description") or "").strip():
            failures.append(f"guide {name!r} has empty description")
        steps = guide.get("steps")
        if not isinstance(steps, list) or not steps:
            failures.append(f"guide {name!r} has no steps")
            continue
        if not isinstance(guide.get("tags"), list):
            failures.append(f"guide {name!r} tags is not a list")
        if _ING_MARKER not in steps:
            failures.append(f"guide {name!r} missing folded ingredients section")

    return failures


def main(argv: list[str]) -> int:
    recipes_path = argv[0] if len(argv) > 0 else "kroger_recipes.json"
    guides_path = argv[1] if len(argv) > 1 else "kroger_guides.json"
    failures = verify(recipes_path, guides_path)
    if failures:
        print("VERIFY FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"VERIFY OK: all {len(TARGET_NAMES)} guides migrated correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
