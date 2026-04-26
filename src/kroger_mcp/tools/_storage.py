"""
JSON file persistence for tool modules.

Owns:
  - JsonStore: read/write a single JSON file path with consistent fallback semantics.

Does NOT own:
  - SQLite/postgres persistence (see analytics/database.py, analytics/pg_database.py).
  - Atomic writes / file locking — single-process MCP server, no concurrent writers.
  - Schema validation — callers handle their own shape (Pydantic at boundaries elsewhere).

Called by (after Phase 2 migration):
  - tools/shared.py            — preferences file
  - tools/cart_tools.py        — cart + order history
  - tools/recipe_tools.py      — recipes
  - tools/shopping_list_tools.py — shopping list
  - tools/auth_tools.py        — auth state

Calls:
  - logging.getLogger only.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStore:
    """Read/write JSON to a fixed path with a default-factory fallback on read.

    Read semantics — never raises:
      - file missing      -> default()
      - file unparseable  -> logs warning, returns default()
      - file unreadable   -> logs warning, returns default()

    Write semantics — raises on disk errors. Callers that want to swallow
    write failures should wrap with the @handle_errors decorator from _errors.py.

    The default factory is invoked on every fallback (not memoized) so callers
    that mutate the returned value don't poison subsequent reads.

    Example:
        cart = JsonStore(Path("kroger_cart.json"), default=lambda: {"items": []})
        data = cart.load()
        cart.save({"items": [...]})
    """

    def __init__(self, path: Path | str, default: Callable[[], Any]) -> None:
        self.path = Path(path)
        self._default = default

    def load(self) -> Any:
        if not self.path.exists():
            return self._default()
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt or unreadable file — surface to logs but don't crash callers.
            # Counterpart write site (save()) raises, so a corrupt read always means
            # an external edit, partial write, or filesystem fault.
            logger.warning("JsonStore: failed to read %s (%s); using default", self.path, exc)
            return self._default()

    def save(self, data: Any) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
