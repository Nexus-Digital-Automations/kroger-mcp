---
title: Backend Hygiene — Errors, Logging, JSON Persistence
status: planning
created: 2026-04-24
---

## Vision
Stop swallowing exceptions silently and stop printing warnings instead of logging them. Centralize JSON file persistence so every tool module uses one tested helper.

## Requirements
1. New `src/kroger_mcp/tools/_storage.py` exposing `JsonStore` with `load()` / `save()` and structured logging on failure.
2. New `src/kroger_mcp/tools/_errors.py` exposing `@handle_errors(default=…, level=…, reraise=…)` decorator.
3. Migrate `_load_*` / `_save_*` helpers in `tools/shared.py`, `tools/cart_tools.py`, `tools/recipe_tools.py`, `tools/shopping_list_tools.py`, `tools/auth_tools.py` to use `JsonStore`.
4. Replace every `print(f"Warning: …")` and `print(…, file=sys.stderr)` under `src/kroger_mcp/**` (except intentional CLI startup output in `web/app.py`) with `logger.warning(…)` / `logger.error(…, exc_info=True)`.
5. Apply `@handle_errors` only at sites where the existing `except Exception` swallowed or `print()`-only-logged the failure. Sites with meaningful recovery logic stay untouched.
6. Unit tests for `_storage.py` and `_errors.py` under `tests/unit/`.

## Acceptance Criteria
- [ ] `src/kroger_mcp/tools/_storage.py` exists with module docstring, type hints, and a `JsonStore` class
- [ ] `src/kroger_mcp/tools/_errors.py` exists with module docstring and `handle_errors` decorator
- [ ] `pytest tests/unit/test_storage.py tests/unit/test_errors.py -v` passes (≥6 cases)
- [ ] `grep -rn "except Exception" src/kroger_mcp --include="*.py" | wc -l` < 50 (was 381)
- [ ] `grep -rn 'print(f"Warning' src/kroger_mcp --include="*.py"` returns 0 lines
- [ ] `grep -rn "file=sys.stderr" src/kroger_mcp --include="*.py"` returns 0 lines (or only in `web/app.py` CLI banner)
- [ ] All five migrated `_load_*`/`_save_*` modules use `JsonStore` (verified by import grep)
- [ ] `node tests/playwright/test_all_features.js` passes after changes (no behavior regression)
- [ ] Lint (Phase 1) still green

## Technical Decisions
- **`JsonStore.load()` returns the `default()` factory result on missing file or `JSONDecodeError`** — never raises for read; logs at WARNING. This matches the existing forgiving behavior of `_load_preferences`.
- **`JsonStore.save()` raises** on disk errors — callers can decide whether to swallow. Existing call sites that print and swallow will wrap in `@handle_errors(default=None)`.
- **Decorator targets functions, not coroutines** in v1 — every current swallow site is sync. If async sites surface, add an async variant in a follow-up.
- **No registry / no DI** — instantiate `JsonStore(path, default)` inline at module load. Single-use objects.

## Progress
- [ ] Spec approved
- [ ] `_storage.py` + tests
- [ ] `_errors.py` + tests
- [ ] Migration sweep
- [ ] Logging cleanup
- [ ] Verification
