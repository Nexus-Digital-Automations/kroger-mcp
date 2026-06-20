#!/usr/bin/env python3
"""One-time data migration: move guide-type records out of the recipe store.

Owner: data migration for the Smart Shopper recipe/guide split.

Three records in ``kroger_recipes.json`` are technique/shopping how-tos, not
meal recipes, so they surface on the Recipes page instead of Guides. This script
moves them into ``kroger_guides.json``, converting recipe schema -> guide schema
with a *faithful flatten*: the markdown ``instructions`` blob becomes a plain-text
step list (guides render steps as a flat numbered <ol>), and the structured
``ingredients`` (including Kroger product_ids / override reasons) are folded into
a labeled section so no content is lost.

Stdlib only (json/re/uuid/datetime) so it runs unmodified on the prod box.
Matches entries by EXACT name (ids differ between local and prod) and is
idempotent: re-running never duplicates a guide.

Usage:
    python3 migrate_guides_from_recipes.py [RECIPES_JSON] [GUIDES_JSON] [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime

TARGET_NAMES = [
    "Master Guide: Cooking Dry Beans with Soaking",
    "Bean Cooking Starter Kit",
    "Quick-Cooking Lentils & Split Peas (No Soak)",
]

_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+")


def _md_line_to_step(raw: str) -> str:
    """Reduce one markdown line to plain text, keeping all words.

    Headers keep their text (act as section labels in the flat list); list
    markers and emphasis are stripped. Numbering is implied by the <ol>.
    """
    s = raw.rstrip().replace("**", "").replace("__", "").replace("`", "")
    header = _HEADER_RE.match(s)
    if header:
        return header.group(1).strip()
    s = _BULLET_RE.sub("", s)
    s = _ORDERED_RE.sub("", s)
    return s.strip()


def _flatten_instructions(instructions: str) -> list[str]:
    """Flatten a markdown instructions blob into one step per non-blank line."""
    steps: list[str] = []
    for raw in (instructions or "").splitlines():
        if not raw.strip():
            continue
        step = _md_line_to_step(raw)
        if step:
            steps.append(step)
    return steps


def _ingredient_to_step(ingredient: dict) -> str:
    """Render a structured ingredient as a single shopping-list step line."""
    qty = ingredient.get("quantity")
    unit = (ingredient.get("unit") or "").strip()
    name = (ingredient.get("name") or "").strip()

    head_parts: list[str] = []
    if qty is not None and qty != "":
        # Tuple form (not int | float) so this runs on the prod box's py3.9 too.
        head_parts.append(f"{qty:g}" if isinstance(qty, (int, float)) else str(qty))  # noqa: UP038
    if unit:
        head_parts.append(unit)
    head = " ".join(head_parts)
    line = f"{head} {name}".strip() if head else name

    extras: list[str] = []
    if ingredient.get("notes"):
        extras.append(str(ingredient["notes"]).strip())
    if ingredient.get("product_id"):
        extras.append(f"Kroger product {ingredient['product_id']}")
    elif ingredient.get("override"):
        reason = ingredient.get("override_reason") or "manual purchase"
        extras.append(f"manual purchase: {reason}")
    if extras:
        line = f"{line} — {'; '.join(extras)}"
    return line


def _normalize_tags(tags) -> list[str]:
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [t for t in tags if isinstance(t, str) and t.strip()]
    return []


def recipe_to_guide(recipe: dict, now: str) -> dict:
    """Convert a recipe record into a guide record (faithful flatten)."""
    steps = _flatten_instructions(recipe.get("instructions") or "")
    ingredients = recipe.get("ingredients") or []
    if ingredients:
        steps.append("— INGREDIENTS / SHOPPING LIST —")
        steps.extend(_ingredient_to_step(i) for i in ingredients)

    return {
        "id": str(uuid.uuid4())[:8],
        "name": recipe.get("name", ""),
        "description": recipe.get("description") or "",
        "steps": steps,
        "tags": _normalize_tags(recipe.get("tags")),
        "time": recipe.get("time") or None,
        "difficulty": recipe.get("difficulty") or None,
        "created_at": recipe.get("created_at") or now,
        "updated_at": now,
        "migrated_from_recipe_id": recipe.get("id"),
    }


def _atomic_write(path: str, data: dict) -> None:
    """Write JSON (JsonStore shape: indent=2, ascii-escaped) atomically."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def migrate(recipes_path: str, guides_path: str, dry_run: bool = False) -> int:
    with open(recipes_path, encoding="utf-8") as fh:
        recipes_doc = json.load(fh)
    with open(guides_path, encoding="utf-8") as fh:
        guides_doc = json.load(fh)

    recipes = recipes_doc.get("recipes", [])
    guides = guides_doc.get("guides", [])
    existing_guide_names = {g.get("name") for g in guides}
    targets = set(TARGET_NAMES)
    now = datetime.now().isoformat()

    kept: list[dict] = []
    moved: list[str] = []
    dropped_dupes: list[str] = []

    for recipe in recipes:
        name = recipe.get("name")
        if name not in targets:
            kept.append(recipe)
            continue
        if name in existing_guide_names:
            # A guide with this name already exists (prior partial run): drop the
            # stray recipe rather than create a duplicate guide.
            dropped_dupes.append(name)
            continue
        guide = recipe_to_guide(recipe, now)
        guides.append(guide)
        existing_guide_names.add(name)
        moved.append(f"{name}  ->  guide {guide['id']} ({len(guide['steps'])} steps)")

    recipes_doc["recipes"] = kept
    if moved or dropped_dupes:
        recipes_doc["last_updated"] = now
        guides_doc["guides"] = guides
        guides_doc["last_updated"] = now

    print(f"recipes: {len(recipes)} -> {len(kept)}")
    print(f"guides:  -> {len(guides)}")
    for line in moved:
        print(f"  MOVED   {line}")
    for name in dropped_dupes:
        print(f"  DROPPED stray recipe (guide already exists): {name}")
    missing = targets - {r.get("name") for r in recipes} - existing_guide_names
    for name in sorted(missing):
        print(f"  WARN    target not found anywhere: {name}")

    if dry_run:
        print("(dry-run: no files written)")
        return 0

    _atomic_write(recipes_path, recipes_doc)
    _atomic_write(guides_path, guides_doc)
    print("written.")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--dry-run"]
    dry_run = "--dry-run" in argv
    recipes_path = args[0] if len(args) > 0 else "kroger_recipes.json"
    guides_path = args[1] if len(args) > 1 else "kroger_guides.json"
    return migrate(recipes_path, guides_path, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
