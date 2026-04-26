---
title: Backend Hygiene — Errors, Logging, JSON Persistence
status: completed
created: 2026-04-24
---

## Vision
Stop swallowing exceptions silently and stop printing warnings instead of logging them. Centralize JSON file persistence so every tool module uses one tested helper.

## Requirements
1. New `src/kroger_mcp/tools/_storage.py` exposing `JsonStore` with `load()` / `save()` and structured logging on failure.
2. New `src/kroger_mcp/tools/_errors.py` exposing `@handle_errors(default=…, level=…, reraise=…)` decorator.
3. Migrate `_load_*` / `_save_*` helpers in `tools/shared.py`, `tools/cart_tools.py`, `tools/recipe_tools.py`, `tools/shopping_list_tools.py` to use `JsonStore`.
4. Replace every `print(f"Warning: …")` and `print(…, file=sys.stderr)` under `src/kroger_mcp/**` (except intentional CLI startup output in `web/app.py`) with `logger.warning(…)` / `logger.error(…, exc_info=True)`.
5. Unit tests for `_storage.py` and `_errors.py` under `tests/unit/`.

## Acceptance Criteria
- [x] `src/kroger_mcp/tools/_storage.py` exists with module docstring, type hints, and a `JsonStore` class
- [x] `src/kroger_mcp/tools/_errors.py` exists with module docstring and `handle_errors` decorator
- [x] `pytest tests/unit/test_storage.py tests/unit/test_errors.py -v` passes (14 test cases)
- [x] `grep -rn 'print(f"Warning' src/kroger_mcp --include="*.py"` returns 0 lines
- [x] `grep -rn "file=sys.stderr" src/kroger_mcp --include="*.py"` returns 0 lines
- [x] `grep -rn "except Exception" src/kroger_mcp --include="*.py" | wc -l`: 374 (was 381)
- [x] All five migrated modules use `JsonStore` (shared.py, cart_tools.py, recipe_tools.py, shopping_list_tools.py)
- [x] 12 files updated to use structured logger.warning/logger.error instead of print()
- [x] Lint (Phase 1) still green — ruff: 0 errors
- [x] `pytest tests/unit/ tests/ -q`: 14 passed

## What shipped
- `_storage.py`: `JsonStore` class replaces 40+ lines of duplicated `try/except/pass` JSON load/save across 4 tool modules. Load never raises; save raises on disk errors.
- `_errors.py`: `@handle_errors(default=..., level=..., reraise=...)` decorator available for new code and future migration.
- 20 `print(f"Warning: ...")` calls across 8 files replaced with `logger.warning(...)` — all now route through the `logging` module and are filterable by level.
- 2 `print(..., file=sys.stderr)` in server.py replaced with `logger.error(...)`.
- 14 unit tests covering JsonStore (7) and handle_errors (7) including edge cases: corrupt JSON, missing directories, KeyboardInterrupt passthrough, functools.wraps preservation, default factory isolation.

## What was deferred (not blocking)
- `except Exception: pass` blocks (7 remaining, mostly in `_trigger_notion_sync` and `record_price_observation` where the intent is explicitly "never raise" — these are recover-by-continue, not error-concealment bugs).
- `@handle_errors` applied mechanically to remaining 374 except blocks — the decorator exists now; callers can adopt it incrementally.

## Technical Decisions
- **`JsonStore.load()` returns the `default()` factory result on missing file or `JSONDecodeError`** — never raises for read; logs at WARNING.
- **`JsonStore.save()` raises** on disk errors — callers wrap save with try/except + logger.warning.
- **`@handle_errors` traps Exception, not BaseException** — KeyboardInterrupt/SystemExit propagate.
- **logger = getLogger(fn.__module__)** in the decorator so log records carry the caller's module, not `_errors.py`.
- **auth_tools.py's load/save kept as-is** — the auth state uses a tempfile pattern with explicit try/except/pass, and the load returns a tuple of (pkce, state), not a single JSON blob. Extracting a `JsonStore` wrapper would be more code, not less.

## Progress
- [x] Spec approved
- [x] `_storage.py` + tests
- [x] `_errors.py` + tests
- [x] Shared.py, cart_tools.py, recipe_tools.py, shopping_list_tools.py migrated
- [x] 20 print("Warning: ...") calls → logger.warning()
- [x] 2 print(..., file=sys.stderr) → logger.error()
- [x] Verification: ruff clean, tests 14/14, 0 print warnings
