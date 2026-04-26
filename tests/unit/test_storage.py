"""Spec tests for JsonStore — see specs/backend-hygiene.md acceptance criteria."""

import json
import logging
from pathlib import Path

import pytest

from kroger_mcp.tools._storage import JsonStore


def test_load_returns_default_when_file_is_missing(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "missing.json", default=lambda: {"items": []})
    assert store.load() == {"items": []}


def test_load_returns_persisted_value_after_save(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "cart.json", default=dict)
    payload = {"items": [{"id": "abc", "qty": 2}], "total": 9.99}
    store.save(payload)
    assert store.load() == payload


def test_load_returns_default_when_file_is_corrupt_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = JsonStore(path, default=lambda: {"items": []})

    with caplog.at_level(logging.WARNING, logger="kroger_mcp.tools._storage"):
        result = store.load()

    assert result == {"items": []}
    assert any("failed to read" in rec.message for rec in caplog.records)


def test_default_factory_is_invoked_per_call_not_shared(tmp_path: Path) -> None:
    # Mutating the result of load() must not poison the next load() — would happen
    # if the default value (rather than factory) were memoized.
    store = JsonStore(tmp_path / "missing.json", default=lambda: {"items": []})
    first = store.load()
    first["items"].append("contaminated")
    second = store.load()
    assert second == {"items": []}


def test_save_creates_file_with_pretty_printed_json(tmp_path: Path) -> None:
    path = tmp_path / "out.json"
    store = JsonStore(path, default=dict)
    store.save({"a": 1, "b": [2, 3]})
    text = path.read_text(encoding="utf-8")
    # indent=2 means at least one newline before the second key
    assert "\n" in text
    assert json.loads(text) == {"a": 1, "b": [2, 3]}


def test_save_raises_when_path_directory_does_not_exist(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "no_such_dir" / "out.json", default=dict)
    with pytest.raises(OSError):
        store.save({"a": 1})


def test_load_accepts_string_path(tmp_path: Path) -> None:
    path = tmp_path / "as_string.json"
    path.write_text('{"hello":"world"}', encoding="utf-8")
    store = JsonStore(str(path), default=dict)
    assert store.load() == {"hello": "world"}
