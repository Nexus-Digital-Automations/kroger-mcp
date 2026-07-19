"""Regression test locking in the background scanner's location env var name.

``scan_watchlist_for_deals()`` used to read ``KROGER_PREFERRED_LOCATION``, but
the production ``.env`` only ever defined ``KROGER_LOCATION_ID`` (the name
every other call site in the app uses --
``tools/shared.py:get_preferred_location_id``). That mismatch silently failed
the deal-watchlist half of every scheduled scan.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

_SCANNER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "background_scanner.py"


def _load_scanner() -> ModuleType:
    """Load the scanner module fresh from its file path (avoids package shadowing)."""
    spec = importlib.util.spec_from_file_location("_scanner_under_test", _SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_scanner_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _patch_scanner_dependencies(monkeypatch, scanner: ModuleType) -> None:
    class _DummyAPI:
        def __init__(self, client_id: str, client_secret: str) -> None:
            pass

    monkeypatch.setattr(scanner, "KrogerAPI", _DummyAPI)
    monkeypatch.setattr(scanner, "ensure_initialized", lambda: None)
    monkeypatch.setenv("KROGER_CLIENT_ID", "test-id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "test-secret")


def test_scan_reads_kroger_location_id(monkeypatch, caplog):
    """KROGER_LOCATION_ID is honored -- the scan proceeds past the location check."""
    scanner = _load_scanner()
    _patch_scanner_dependencies(monkeypatch, scanner)
    monkeypatch.delenv("KROGER_PREFERRED_LOCATION", raising=False)
    monkeypatch.setenv("KROGER_LOCATION_ID", "03400014")

    def _stop(*args, **kwargs):
        raise RuntimeError("stop-after-location-check")

    monkeypatch.setattr(scanner, "get_db_connection", _stop)

    with caplog.at_level(logging.ERROR):
        try:
            scanner.scan_watchlist_for_deals()
        except RuntimeError:
            pass

    assert "No KROGER_LOCATION_ID set" not in caplog.text


def test_scan_errors_without_kroger_location_id(monkeypatch, caplog):
    """The old KROGER_PREFERRED_LOCATION name alone is no longer honored --
    locks in the fix so a future revert is caught immediately."""
    scanner = _load_scanner()
    _patch_scanner_dependencies(monkeypatch, scanner)
    monkeypatch.delenv("KROGER_LOCATION_ID", raising=False)
    monkeypatch.setenv("KROGER_PREFERRED_LOCATION", "03400014")

    with caplog.at_level(logging.ERROR):
        scanner.scan_watchlist_for_deals()

    assert "No KROGER_LOCATION_ID set" in caplog.text
