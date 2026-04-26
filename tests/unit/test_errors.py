"""Spec tests for @handle_errors — see specs/backend-hygiene.md acceptance criteria."""

import logging

import pytest

from kroger_mcp.tools._errors import handle_errors


def test_returns_function_result_on_success() -> None:
    @handle_errors(default=-1)
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_returns_default_when_wrapped_function_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @handle_errors(default={"items": []})
    def explode() -> dict:
        raise RuntimeError("disk on fire")

    with caplog.at_level(logging.WARNING):
        assert explode() == {"items": []}
    assert any("disk on fire" in rec.message or rec.exc_info for rec in caplog.records)


def test_logs_at_configured_level(caplog: pytest.LogCaptureFixture) -> None:
    @handle_errors(default=None, level=logging.ERROR)
    def explode() -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR):
        explode()
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_reraises_when_configured(caplog: pytest.LogCaptureFixture) -> None:
    @handle_errors(default=None, reraise=True)
    def explode() -> None:
        raise KeyError("missing")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyError):
            explode()


def test_does_not_swallow_keyboardinterrupt() -> None:
    # Catching BaseException would break Ctrl-C and shutdown signals.
    # @handle_errors must catch Exception only.
    @handle_errors(default="swallowed")
    def explode() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        explode()


def test_log_record_originates_from_wrapped_function_module(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The decorator uses getLogger(fn.__module__), so log records should be
    # filterable by the originating module rather than _errors.py itself.
    @handle_errors(default=None)
    def explode() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.WARNING):
        explode()
    assert any(rec.name == __name__ for rec in caplog.records)


def test_preserves_function_metadata() -> None:
    @handle_errors(default=None)
    def my_named_function() -> int:
        """Docstring stays."""
        return 42

    assert my_named_function.__name__ == "my_named_function"
    assert my_named_function.__doc__ == "Docstring stays."
