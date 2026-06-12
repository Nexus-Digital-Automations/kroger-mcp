"""Step-time extraction, overrides, totals, formatting.

Pins the parsing contract from specs/recipe-step-times.md: per-step value is
the LARGEST duration mentioned, ranges take the upper bound, overnight = 8 h
passive, and overrides (content-hash keyed) beat auto-detection.
"""

from __future__ import annotations

from kroger_mcp.tools.step_times import (
    annotate_steps,
    extract_step_time,
    format_minutes,
    recipe_time_summary,
    step_time_key,
)

# --- extraction ---------------------------------------------------------------


def test_simple_minutes():
    assert extract_step_time("Simmer the sauce for 20 minutes.") == {
        "minutes": 20,
        "passive": False,
    }


def test_hours_convert_to_minutes():
    assert extract_step_time("Roast for 1.5 hours.")["minutes"] == 90


def test_range_takes_upper_bound():
    assert extract_step_time("Sauté 5 to 7 minutes until golden.")["minutes"] == 7
    assert extract_step_time("Bake 25-30 min.")["minutes"] == 30


def test_hyphenated_unit_form():
    assert extract_step_time("Give it a 5-minute rest off heat.")["minutes"] == 5


def test_multiple_durations_take_max_not_sum():
    # "rotating after 10" must not double-count.
    assert extract_step_time("Bake 20 minutes, rotating after 10 minutes.")[
        "minutes"
    ] == 20


def test_overnight_is_eight_hours_passive():
    res = extract_step_time("Marinate the chicken overnight.")
    assert res == {"minutes": 480, "passive": True}


def test_passive_verbs_detected():
    assert extract_step_time("Chill the dough for 2 hours.")["passive"] is True
    assert extract_step_time("Let it rest 10 minutes.")["passive"] is True
    assert extract_step_time("Boil pasta 9 minutes.")["passive"] is False


def test_no_time_and_temperature_only_yield_none():
    assert extract_step_time("Season generously with salt and pepper.") is None
    assert extract_step_time("Preheat the oven to 350 degrees.") is None
    assert extract_step_time("") is None


def test_absurd_durations_ignored():
    assert extract_step_time("Ferment for 9000 hours.") is None


# --- formatting ----------------------------------------------------------------


def test_format_minutes_labels():
    assert format_minutes(45) == "45 min"
    assert format_minutes(70) == "1 h 10 min"
    assert format_minutes(480) == "8 h"
    assert format_minutes(0) == ""
    assert format_minutes(None) == ""


# --- annotation + overrides -----------------------------------------------------


def test_annotate_totals_split_active_passive():
    steps = [
        "Sauce:",  # header line — no time
        "Simmer 20 minutes.",
        "Chill for 1 hour.",
    ]
    out = annotate_steps(steps, None)
    assert out["times"][0].get("minutes") is None
    assert out["times"][1]["minutes"] == 20 and not out["times"][1]["passive"]
    assert out["times"][2]["minutes"] == 60 and out["times"][2]["passive"]
    assert out["totals"] == {
        "active": 20,
        "passive": 60,
        "total": 80,
        "active_label": "20 min",
        "passive_label": "1 h",
        "total_label": "1 h 20 min",
    }


def test_override_beats_auto_and_keys_are_content_hashed():
    step = "Simmer 20 minutes."
    key = step_time_key(step)
    out = annotate_steps([step], {key: {"minutes": 35, "passive": True}})
    assert out["times"][0]["minutes"] == 35
    assert out["times"][0]["passive"] is True
    assert out["times"][0]["source"] == "override"
    # Same text, different whitespace/case → same key (reorder-safe).
    assert step_time_key("  simmer   20 MINUTES. ") == key
    # Edited text → different key → override intentionally dropped.
    out2 = annotate_steps(["Simmer 25 minutes."], {key: {"minutes": 35}})
    assert out2["times"][0]["source"] == "auto"
    assert out2["times"][0]["minutes"] == 25


def test_recipe_time_summary_explicit_total_wins():
    recipe = {
        "instructions": "Simmer 20 minutes.\nChill for 1 hour.",
        "total_time_minutes": 50,
    }
    summary = recipe_time_summary(recipe)
    assert summary["total"] == 50
    assert summary["explicit"] is True
    assert summary["label"] == "50 min"
    # Without the explicit field the derived sum is used.
    del recipe["total_time_minutes"]
    derived = recipe_time_summary(recipe)
    assert derived["total"] == 80
    assert derived["explicit"] is False
    assert derived["active"] == 20 and derived["passive"] == 60


def test_recipe_time_summary_handles_json_array_instructions():
    recipe = {"instructions": '["Boil 9 minutes.", "Drain and serve."]'}
    assert recipe_time_summary(recipe)["total"] == 9
