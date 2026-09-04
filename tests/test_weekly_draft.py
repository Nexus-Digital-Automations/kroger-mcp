"""Weekly auto-draft generation + configurable week boundaries.

Critical-path coverage (data integrity): drafts must never deduct pantry, the
draft generator must be idempotent (no duplicate plans from repeated calls),
and week-boundary math must honor the user's configured start day — a wrong
boundary silently plans (and later deducts) the wrong week.
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _pg_support import skip_on_pg

from kroger_mcp.analytics import meal_planning
from kroger_mcp.analytics.database import (
    ensure_initialized,
    get_db_connection,
    reset_initialization,
)
from kroger_mcp.analytics.meal_planning import (
    approve_draft,
    assign_meal,
    create_meal_plan,
    generate_draft,
    get_meal_plans,
    week_start_for_date,
)
from kroger_mcp.analytics.notifications import (
    draft_awaiting_approval,
    next_week_needs_plan,
)
from kroger_mcp.tools.shared import (
    get_week_start_day,
    set_draft_auto_approve,
    set_draft_dinners_per_week,
    set_planning_horizon_days,
    set_week_start_day,
)

pytestmark = skip_on_pg


def _user() -> str:
    return os.environ["KROGER_MCP_DEFAULT_USER_ID"]


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    import importlib

    db = importlib.import_module("kroger_mcp.analytics.database")
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "weekly_draft_test.db"))
    reset_initialization()
    ensure_initialized()
    yield
    reset_initialization()


def _fake_recipes(*ids):
    return {rid: {"id": rid, "name": f"Recipe {rid}", "servings": 4} for rid in ids}


def _draft_start() -> date:
    return week_start_for_date(datetime.now().date(), 6) + timedelta(days=7)


def _plan_rows():
    conn = get_db_connection()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, is_draft, start_date FROM meal_plans"
            ).fetchall()
        ]
    finally:
        conn.close()


def _entry_dates(plan_id):
    conn = get_db_connection()
    try:
        return [
            (r["meal_date"], r["meal_slot"], r["recipe_id"])
            for r in conn.execute(
                "SELECT meal_date, meal_slot, recipe_id FROM meal_entries "
                "WHERE plan_id = ? ORDER BY meal_date",
                (plan_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ── week_start_for_date ──────────────────────────────────────────────────────

def test_week_start_sunday_convention():
    # 2026-09-02 is a Wednesday; Sunday-start week began 2026-08-30.
    wednesday = date(2026, 9, 2)
    assert week_start_for_date(wednesday, 6) == date(2026, 8, 30)
    # A Sunday is its own week start.
    assert week_start_for_date(date(2026, 8, 30), 6) == date(2026, 8, 30)
    # Saturday is the LAST day of a Sunday-start week, not the first.
    assert week_start_for_date(date(2026, 9, 5), 6) == date(2026, 8, 30)


def test_week_start_monday_convention():
    wednesday = date(2026, 9, 2)
    assert week_start_for_date(wednesday, 0) == date(2026, 8, 31)
    assert week_start_for_date(date(2026, 8, 31), 0) == date(2026, 8, 31)
    # Sunday belongs to the week that started the PREVIOUS Monday.
    assert week_start_for_date(date(2026, 9, 6), 0) == date(2026, 8, 31)


def test_week_start_preserves_datetime_type():
    dt = datetime(2026, 9, 2, 15, 30)
    result = week_start_for_date(dt, 6)
    assert isinstance(result, datetime)
    assert result.date() == date(2026, 8, 30)


# ── generate_draft ───────────────────────────────────────────────────────────

def test_generate_draft_fills_dinner_slots(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1", "r2", "r3")
    )
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["is_draft"] is True
    assert result["assigned"] == 3  # default draft_dinners_per_week
    assert "(draft)" in _plan_rows()[0]["name"]

    entries = _entry_dates(result["plan_id"])
    assert all(slot == "dinner" for _, slot, _ in entries)
    # 3 dinners spread across a 7-day horizon: offsets 0, 2, 5.
    start = _draft_start()
    expected = {
        (start + timedelta(days=o)).isoformat() for o in (0, 2, 5)
    }
    assert {d for d, _, _ in entries} == expected


def test_generate_draft_is_idempotent(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1", "r2", "r3")
    )
    first = generate_draft(user_id=_user())
    second = generate_draft(user_id=_user())

    assert second["success"] is True
    assert second["already_drafted"] is True
    assert second["plan_id"] == first["plan_id"]
    assert len(_plan_rows()) == 1  # no re-roll, no duplicate plan


def test_generate_draft_zero_recipes_errors(clean_db, monkeypatch):
    monkeypatch.setattr(meal_planning, "_get_recipes_index", lambda: {})
    result = generate_draft(user_id=_user())
    assert result["success"] is False
    assert "recipe" in result["error"].lower()
    assert _plan_rows() == []


def test_generate_draft_skips_already_planned_week(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1")
    )
    start = _draft_start()
    create_meal_plan(
        "Real Plan",
        (start - timedelta(days=1)).isoformat(),
        (start + timedelta(days=5)).isoformat(),
        plan_type="custom",
        user_id=_user(),
    )

    result = generate_draft(user_id=_user())
    assert result["success"] is True
    assert result["already_planned"] is True
    assert len(_plan_rows()) == 1  # no draft created


def test_generate_draft_respects_dinners_setting(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1", "r2", "r3")
    )
    set_draft_dinners_per_week(2, user_id=_user())
    result = generate_draft(user_id=_user())
    assert result["assigned"] == 2


def test_generate_draft_avoids_recent_repeats(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning,
        "_get_recipes_index",
        lambda: _fake_recipes("r1", "r2", "r3", "r4"),
    )
    # r1 was cooked recently in a real (non-draft) plan. The plan must END
    # before next week's start or generate_draft would see the week as
    # already planned.
    today = datetime.now().date()
    plan = create_meal_plan(
        "This Week",
        (today - timedelta(days=6)).isoformat(),
        today.isoformat(),
        plan_type="custom",
        user_id=_user(),
    )
    assign_meal(
        plan["plan_id"], "r1", (today - timedelta(days=1)).isoformat(), "dinner",
        user_id=_user(),
    )

    result = generate_draft(user_id=_user())
    drafted = {rid for _, _, rid in _entry_dates(result["plan_id"])}
    assert "r1" not in drafted  # 3 never-used recipes outrank the recent one
    assert drafted == {"r2", "r3", "r4"}


# ── approve_draft / visibility ───────────────────────────────────────────────

def test_draft_hidden_from_list_until_approved(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1")
    )
    result = generate_draft(user_id=_user())
    plan_id = result["plan_id"]

    listed = get_meal_plans(include_past=True, user_id=_user())
    assert all(p["id"] != plan_id for p in listed["plans"])

    approved = approve_draft(plan_id, user_id=_user())
    assert approved["success"] is True

    listed = get_meal_plans(include_past=True, user_id=_user())
    assert any(p["id"] == plan_id for p in listed["plans"])


def test_approve_draft_rejects_non_draft(clean_db):
    plan = create_meal_plan(
        "Normal", "2026-09-06", "2026-09-12", plan_type="weekly", user_id=_user()
    )
    result = approve_draft(plan["plan_id"], user_id=_user())
    assert result["success"] is False

    missing = approve_draft("no-such-plan", user_id=_user())
    assert missing["success"] is False


# ── auto-approve (opt-in full passivity) ─────────────────────────────────────

def test_auto_approve_creates_live_plan(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1", "r2", "r3")
    )
    set_draft_auto_approve(1, user_id=_user())
    result = generate_draft(user_id=_user())

    assert result["success"] is True
    assert result["is_draft"] is False
    assert result["auto_approved"] is True
    rows = _plan_rows()
    # is_draft=0 is the single gate list_pending_meals joins on, so a live-born
    # plan reconciles (auto-deducts) exactly like an approved one.
    assert rows[0]["is_draft"] == 0
    assert "(draft)" not in rows[0]["name"]
    listed = get_meal_plans(include_past=True, user_id=_user())
    assert any(p["id"] == result["plan_id"] for p in listed["plans"])
    assert next_week_needs_plan(_user()) is False

    second = generate_draft(user_id=_user())  # idempotent via already_planned
    assert second["already_planned"] is True
    assert len(_plan_rows()) == 1


def test_auto_approve_does_not_retro_approve_existing_draft(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1")
    )
    first = generate_draft(user_id=_user())  # setting off -> draft created
    set_draft_auto_approve(1, user_id=_user())

    second = generate_draft(user_id=_user())
    assert second["already_drafted"] is True
    assert second["plan_id"] == first["plan_id"]
    assert _plan_rows()[0]["is_draft"] == 1  # still needs one explicit approval


# ── draft_awaiting_approval bell helper ──────────────────────────────────────

def test_bell_reports_draft_awaiting_approval(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1")
    )
    assert draft_awaiting_approval(_user()) is None

    result = generate_draft(user_id=_user())
    pending = draft_awaiting_approval(_user())
    assert pending is not None
    assert pending["plan_id"] == result["plan_id"]
    assert next_week_needs_plan(_user()) is True  # a draft isn't coverage yet

    approve_draft(result["plan_id"], user_id=_user())
    assert draft_awaiting_approval(_user()) is None


# ── next_week_needs_plan with configurable week start ────────────────────────

def test_next_week_needs_plan_honors_sunday_start(clean_db):
    assert get_week_start_day(user_id=_user()) == 6  # default is Sunday
    assert next_week_needs_plan(_user()) is True

    start = _draft_start()  # next Sunday
    create_meal_plan(
        "Next Week",
        start.isoformat(),
        (start + timedelta(days=6)).isoformat(),
        plan_type="weekly",
        user_id=_user(),
    )
    assert next_week_needs_plan(_user()) is False


def test_next_week_needs_plan_ignores_unapproved_draft(clean_db, monkeypatch):
    monkeypatch.setattr(
        meal_planning, "_get_recipes_index", lambda: _fake_recipes("r1")
    )
    result = generate_draft(user_id=_user())
    assert next_week_needs_plan(_user()) is True  # draft doesn't count

    approve_draft(result["plan_id"], user_id=_user())
    assert next_week_needs_plan(_user()) is False


def test_settings_validation(clean_db):
    with pytest.raises(ValueError):
        set_week_start_day(7, user_id=_user())
    with pytest.raises(ValueError):
        set_planning_horizon_days(0, user_id=_user())
    with pytest.raises(ValueError):
        set_draft_dinners_per_week(8, user_id=_user())
    with pytest.raises(ValueError):
        set_draft_auto_approve(2, user_id=_user())
