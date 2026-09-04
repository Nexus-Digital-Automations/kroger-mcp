"""
Gemma-backed dinner selection for the weekly draft.

generate_draft() asks Gemma (Google's open model, via the shared
OpenAI-compatible client and the free-tier GEMINI_API_KEY) to pick which N
saved recipes best fit the upcoming week — current season, upcoming holidays —
with a one-line reason per pick. The model chooses only from saved recipes;
it never invents new ones.

Failure contract: select_dinners_with_llm() returns None on ANY failure
(missing key, provider error, malformed JSON, unknown recipe ids, too few
picks) and never raises. The caller falls back to recency rotation, so the
weekly workflow keeps working without an LLM.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from kroger_mcp.llm_client import OpenAICompatibleClient

from .seasonal import get_upcoming_holidays

logger = logging.getLogger(__name__)

DRAFT_PROVIDER = "gemma4"
MAX_CATALOG_RECIPES = 100
MAX_INGREDIENTS_PER_RECIPE = 10
MAX_REASON_CHARS = 200

_SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
}


def season_for_month(month: int) -> str:
    """Northern-hemisphere season label for a calendar month (1-12)."""
    return _SEASONS[month]


def _chat_completion(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One Gemma chat call. Test seam — tests monkeypatch this function.

    A fresh client (not the web layer's cache) so the key is read at call
    time; the client returns an error dict rather than raising.
    """
    return OpenAICompatibleClient(DRAFT_PROVIDER).chat(messages)


def build_selection_prompt(
    recipes: list[dict[str, Any]],
    dinner_count: int,
    draft_start: datetime,
    horizon_days: int,
    recently_used: dict[str, str],
) -> list[dict[str, Any]]:
    """Messages asking Gemma to pick dinner_count recipe ids with reasons."""
    today = datetime.now().date()
    week_end = draft_start + timedelta(days=horizon_days - 1)
    holidays = get_upcoming_holidays(days_ahead=horizon_days + 21)
    holiday_lines = [
        f"- {h['holiday']} on {h['holiday_date']}" for h in holidays
    ] or ["- none in the next few weeks"]

    catalog_lines = []
    for recipe in recipes[:MAX_CATALOG_RECIPES]:
        names = [
            ing.get("name", "")
            for ing in recipe.get("ingredients", [])
            if isinstance(ing, dict) and ing.get("name")
        ][:MAX_INGREDIENTS_PER_RECIPE]
        line = f"- id={recipe['id']} | {recipe.get('name', 'Unnamed')}"
        if names:
            line += f" | ingredients: {', '.join(names)}"
        if recipe["id"] in recently_used:
            line += f" | last cooked {recently_used[recipe['id']]}"
        catalog_lines.append(line)

    system = (
        "You are a meal-planning assistant. Pick dinners from the user's saved "
        "recipes only — never invent a recipe or id. Respond with ONLY a JSON "
        "object, no prose and no markdown fences, in exactly this shape: "
        '{"selections": [{"recipe_id": "<id from the catalog>", '
        '"reason": "<one short line>"}]}'
    )
    user = (
        f"Today is {today.isoformat()} ({season_for_month(today.month)}).\n"
        f"Plan {dinner_count} dinners for the week of "
        f"{draft_start.date().isoformat()} to {week_end.date().isoformat()}.\n"
        f"Upcoming holidays:\n" + "\n".join(holiday_lines) + "\n\n"
        "Favor seasonal ingredients and holiday fit; avoid recipes cooked "
        "recently unless the seasonal fit is strong. Pick exactly "
        f"{dinner_count} distinct recipes from this catalog:\n"
        + "\n".join(catalog_lines)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_selection(
    content: str, valid_ids: set[str], dinner_count: int
) -> list[dict[str, str]] | None:
    """Validate the model's JSON into exactly dinner_count unique picks.

    Tolerates markdown fences around the JSON. Unknown ids and duplicates are
    dropped (order preserved); fewer than dinner_count survivors → None.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):]
        text = text.strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    selections = payload.get("selections") if isinstance(payload, dict) else None
    if not isinstance(selections, list):
        return None

    picks: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in selections:
        if not isinstance(entry, dict):
            continue
        recipe_id = str(entry.get("recipe_id", ""))
        if recipe_id not in valid_ids or recipe_id in seen:
            continue
        seen.add(recipe_id)
        reason = str(entry.get("reason", "")).strip()[:MAX_REASON_CHARS]
        picks.append({"recipe_id": recipe_id, "reason": reason})
    if len(picks) < dinner_count:
        return None
    return picks[:dinner_count]


def select_dinners_with_llm(
    recipes: list[dict[str, Any]],
    dinner_count: int,
    draft_start: datetime,
    horizon_days: int,
    recently_used: dict[str, str],
) -> list[dict[str, str]] | None:
    """Gemma's pick of dinner_count recipes, or None to fall back to rotation."""
    messages = build_selection_prompt(
        recipes, dinner_count, draft_start, horizon_days, recently_used
    )
    response = _chat_completion(messages)
    if response.get("error"):
        logger.warning(
            "draft selection: provider unavailable (%s); falling back to rotation",
            response.get("message"),
        )
        return None
    try:
        content = response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        logger.warning("draft selection: unexpected response shape; falling back")
        return None
    picks = parse_selection(
        content, {r["id"] for r in recipes}, dinner_count
    )
    if picks is None:
        logger.warning("draft selection: unusable model output; falling back")
        return None
    logger.info(
        "draft selection: gemma picked %s dinners", len(picks)
    )
    return picks
