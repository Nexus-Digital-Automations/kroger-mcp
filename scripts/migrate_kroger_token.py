"""One-shot migration: legacy shared token file -> encrypted per-user DB row.

Moves the single shared ``.kroger_token_user.json`` (the source of the
multi-user overwrite bug) into the encrypted ``kroger_tokens`` table, keyed by
the migration-installed default owner. After a successful import the file is
renamed to ``.kroger_token_user.json.migrated`` so it is never read again.

Idempotent:
  - If the file is already ``.migrated`` (or absent), do nothing.
  - If the owner already has a DB token, do nothing (don't clobber a newer one).

Run with:
    uv run python scripts/migrate_kroger_token.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Make ``import kroger_mcp`` work when run as a bare script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kroger_mcp.auth.dependencies import default_user_id  # noqa: E402
from kroger_mcp.auth.kroger_tokens import (  # noqa: E402
    load_kroger_token,
    save_kroger_token,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_kroger_token")

_TOKEN_FILE = _REPO_ROOT / ".kroger_token_user.json"
_MIGRATED_FILE = _REPO_ROOT / ".kroger_token_user.json.migrated"


def migrate() -> int:
    """Run the migration. Returns a process exit code (0 = success/no-op)."""
    if not _TOKEN_FILE.exists():
        logger.info(
            "no legacy token file at %s (already migrated or never authenticated) — nothing to do",
            _TOKEN_FILE,
        )
        return 0

    try:
        owner = default_user_id()
    except RuntimeError as exc:
        logger.error("cannot resolve migration owner: %s", exc)
        return 1

    if load_kroger_token(owner) is not None:
        logger.info(
            "owner=%s already has a DB token — leaving legacy file in place, no overwrite",
            owner,
        )
        return 0

    try:
        token_info = json.loads(_TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("could not read legacy token file %s: %s", _TOKEN_FILE, exc)
        return 1

    if "access_token" not in token_info:
        logger.error("legacy token file %s has no access_token — refusing to migrate", _TOKEN_FILE)
        return 1

    save_kroger_token(owner, token_info)
    _TOKEN_FILE.rename(_MIGRATED_FILE)
    logger.info(
        "migrated kroger token for owner=%s into kroger_tokens; renamed file -> %s",
        owner,
        _MIGRATED_FILE.name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(migrate())
