"""Tests for the guides feature: storage round-trip, list payload, and create API.

Guides are global JSON-backed records (kroger_guides.json) mirroring recipes but
trimmed to technique how-tos. These tests isolate the store to a tmp file so they
never touch the real data file.
"""

from __future__ import annotations

import asyncio

import pytest

import kroger_mcp.tools.guide_tools as gt
from kroger_mcp.tools._storage import JsonStore


@pytest.fixture
def tmp_guides(tmp_path, monkeypatch):
    """Point the guide store at an isolated tmp file."""
    path = tmp_path / "kroger_guides.json"
    store = JsonStore(str(path), default=lambda: {"guides": [], "last_updated": None})
    monkeypatch.setattr(gt, "_guides_store", store)
    monkeypatch.setattr(gt, "_guides_cache", None)
    return path


def test_save_load_find_round_trip(tmp_guides):
    data = gt._load_guides()
    assert data["guides"] == []

    data["guides"].append(
        {
            "id": "guide123",
            "name": "Soaking Beans",
            "description": "How to soak beans.",
            "steps": ["Rinse", "Cover with water", "Soak overnight"],
            "tags": ["beans", "prep"],
            "time": "8 h",
            "difficulty": "easy",
            "created_at": "2026-06-18T09:00:00",
            "updated_at": "2026-06-18T09:00:00",
        }
    )
    gt._save_guides(data)

    found = gt._find_guide("guide123")
    assert found is not None
    assert found["name"] == "Soaking Beans"
    assert len(found["steps"]) == 3
    assert gt._find_guide("nope") is None


def test_normalize_steps_strips_and_filters():
    assert gt._normalize_steps(["a", "  ", "b "]) == ["a", "b"]
    assert gt._normalize_steps("one\ntwo\n") == ["one", "two"]
    assert gt._normalize_steps(None) == []


def test_guides_payload_shape(tmp_guides):
    from kroger_mcp.web.routes.guides import _guides_payload

    gt._save_guides(
        {
            "guides": [
                {
                    "id": "g1",
                    "name": "Dice an Onion",
                    "description": "Knife technique.",
                    "steps": ["Halve", "Slice", "Dice"],
                    "tags": ["knife-skills"],
                    "time": "5 min",
                    "difficulty": "easy",
                }
            ]
        }
    )

    from kroger_mcp.auth.dependencies import default_user_id

    payload = _guides_payload(default_user_id())
    assert payload["active_page"] == "guides"
    assert payload["guide_count"] == 1
    # guides_data is a Python list (template serializes it with the script-safe
    # `tojson` filter), not a pre-serialized JSON string.
    assert payload["guides_data"][0]["name"] == "Dice an Onion"
    assert payload["guides_data"][0]["step_count"] == 3
    assert "knife-skills" in payload["all_tags"]


def test_create_guide_api_round_trip(tmp_guides):
    from kroger_mcp.web.routes.api.guides import CreateGuideBody, create_guide

    body = CreateGuideBody(
        name="Fresh Pasta",
        description="Egg pasta by hand.",
        steps=["Mound flour", "Add eggs", "Knead"],
        tags=["pasta"],
        time="1 h",
        difficulty="medium",
    )
    result = asyncio.run(create_guide(body))
    assert result["success"] is True
    new_id = result["guide_id"]

    saved = gt._find_guide(new_id)
    assert saved is not None
    assert saved["name"] == "Fresh Pasta"
    assert saved["steps"] == ["Mound flour", "Add eggs", "Knead"]
    assert saved["difficulty"] == "medium"
