"""Gemma-backed seasonal dinner selection for the weekly draft.

Critical-path coverage (data integrity): the LLM must never be able to break
the weekly draft — every failure mode (provider error, malformed JSON, unknown
recipe ids, raised exception, missing API key) must silently fall back to the
recency rotation and still produce a draft. A successful selection must use
exactly the model's picks and persist its reasons as meal-entry notes.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _pg_support import skip_on_pg

from kroger_mcp.analytics import draft_selection, meal_planning
from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    reset_initialization,
)
from kroger_mcp.analytics.draft_selection import parse_selection, season_for_month
from kroger_mcp.analytics.meal_planning import generate_draft

pytestmark = skip_on_pg


def _user() -> str:
    return os.environ["KROGER_MCP_DEFAULT_USER_ID"]


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "gemma_draft_test.db"))
    reset_initialization()
    ensure_initialized()
    yield
    reset_initialization()


@pytest.fixture
def recipes(monkeypatch):
    index = {
        rid: {
            "id": rid,
            "name": f"Recipe {rid}",
            "servings": 4,
            "ingredients": [{"name": f"ingredient-{rid}"}],
        }
        for rid in ("r1", "r2", "r3", "r4")
    }
    monkeypatch.setattr(meal_planning, "_get_recipes_index", lambda: index)
    return index


def _gemma_reply(*picks: tuple[str, str]) -> dict:
    content = json.dumps(
        {"selections": [{"recipe_id": rid, "reason": why} for rid, why in picks]}
    )
    return {"choices": [{"message": {"content": content}}]}


def _draft_entries(plan_id):
    conn = get_db_connection()
    try:
        return [
            (r["recipe_id"], r["notes"])
            for r in conn.execute(
                "SELECT recipe_id, notes FROM meal_entries "
                "WHERE plan_id = ? ORDER BY meal_date",
                (plan_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ── successful selection ─────────────────────────────────────────────────────

def test_gemma_picks_are_used_in_order_with_notes(clean_db, recipes, monkeypatch):
    monkeypatch.setattr(
        draft_selection,
        "_chat_completion",
        lambda messages: _gemma_reply(
            ("r3", "peak tomato season"),
            ("r1", "fits Labor Day"),
            ("r4", "hearty for fall"),
        ),
    )
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["selection_mode"] == "gemma"
    entries = _draft_entries(result["plan_id"])
    assert [rid for rid, _ in entries] == ["r3", "r1", "r4"]
    assert dict(entries)["r3"] == "peak tomato season"


def test_thought_wrapped_reply_still_used(clean_db, recipes, monkeypatch):
    """Gemma 4 wraps replies in <thought> blocks (observed live 2026-09-05);
    the picks inside must still be used, not silently rotated away."""
    inner = json.dumps(
        {
            "selections": [
                {"recipe_id": "r2", "reason": "squash season"},
                {"recipe_id": "r4", "reason": "cool-weather braise"},
                {"recipe_id": "r1", "reason": "weeknight quick"},
            ]
        }
    )
    content = (
        "<thought>Constraints: {3 dinners}. Catalog has r1..r4.\n"
        "I will favor fall ingredients.</thought>\n"
        "Here is the selection:\n```json\n" + inner + "\n```"
    )
    monkeypatch.setattr(
        draft_selection,
        "_chat_completion",
        lambda messages: {"choices": [{"message": {"content": content}}]},
    )
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["selection_mode"] == "gemma"
    assert [rid for rid, _ in _draft_entries(result["plan_id"])] == ["r2", "r4", "r1"]


def test_prompt_carries_season_and_catalog(recipes):
    messages = draft_selection.build_selection_prompt(
        list(recipes.values()),
        dinner_count=2,
        draft_start=datetime.now() + timedelta(days=7),
        horizon_days=7,
        recently_used={"r2": "2026-08-30"},
    )
    user_msg = messages[1]["content"]
    assert season_for_month(datetime.now().month) in user_msg
    assert "id=r1" in user_msg and "ingredient-r1" in user_msg
    assert "last cooked 2026-08-30" in user_msg
    assert "ONLY a JSON object" in messages[0]["content"]


# ── every failure mode falls back to rotation, draft still created ───────────

@pytest.mark.parametrize(
    "reply",
    [
        {"error": True, "message": "API key not configured"},
        {"choices": [{"message": {"content": "not json at all"}}]},
        {"choices": [{"message": {"content": '{"selections": []}'}}]},
        _gemma_reply(("nope-1", "x"), ("nope-2", "y"), ("nope-3", "z")),
        _gemma_reply(("r1", "only one pick")),
        {"unexpected": "shape"},
    ],
    ids=["error-dict", "bad-json", "empty", "invalid-ids", "too-few", "bad-shape"],
)
def test_unusable_llm_output_falls_back_to_rotation(clean_db, recipes, monkeypatch, reply):
    monkeypatch.setattr(draft_selection, "_chat_completion", lambda messages: reply)
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["selection_mode"] == "rotation"
    assert result["assigned"] == 3  # default draft_dinners_per_week


def test_llm_exception_falls_back_to_rotation(clean_db, recipes, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("selection blew up")

    monkeypatch.setattr(draft_selection, "select_dinners_with_llm", _boom)
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["selection_mode"] == "rotation"
    assert result["assigned"] == 3


def test_missing_api_key_never_hits_network(clean_db, recipes):
    # conftest strips GEMINI_API_KEY; the client returns an error dict before
    # any network I/O, so this passing (fast, offline) IS the assertion.
    assert os.environ.get("GEMINI_API_KEY") is None
    result = generate_draft(user_id=_user())
    assert result["success"] is True
    assert result["selection_mode"] == "rotation"


# ── parse_selection ──────────────────────────────────────────────────────────

def test_parse_selection_strips_fences_dedupes_and_truncates():
    content = (
        "```json\n"
        + json.dumps(
            {
                "selections": [
                    {"recipe_id": "r1", "reason": "x" * 500},
                    {"recipe_id": "r1", "reason": "duplicate"},
                    {"recipe_id": "bogus", "reason": "unknown id"},
                    {"recipe_id": "r2", "reason": "fine"},
                ]
            }
        )
        + "\n```"
    )
    picks = parse_selection(content, {"r1", "r2"}, dinner_count=2)
    assert picks is not None
    assert [p["recipe_id"] for p in picks] == ["r1", "r2"]
    assert len(picks[0]["reason"]) == draft_selection.MAX_REASON_CHARS


def test_parse_selection_survives_thought_blocks_and_prose():
    inner = json.dumps({"selections": [{"recipe_id": "r1", "reason": "ok"}]})
    wrapped = (
        "<thought>brace noise: {\"selections\": []} inside reasoning</thought>\n"
        "Sure! " + inner + "\nHope that helps."
    )
    picks = parse_selection(wrapped, {"r1"}, dinner_count=1)
    assert picks is not None and picks[0]["recipe_id"] == "r1"
    assert parse_selection("<thought>only thinking, no json</thought>", {"r1"}, 1) is None


def test_parse_selection_requires_enough_valid_picks():
    content = json.dumps({"selections": [{"recipe_id": "r1", "reason": "ok"}]})
    assert parse_selection(content, {"r1", "r2"}, dinner_count=2) is None
    assert parse_selection("[]", {"r1"}, dinner_count=1) is None


def test_season_for_month_covers_all_months():
    assert season_for_month(1) == "winter"
    assert season_for_month(4) == "spring"
    assert season_for_month(7) == "summer"
    assert season_for_month(10) == "fall"
    assert season_for_month(12) == "winter"
