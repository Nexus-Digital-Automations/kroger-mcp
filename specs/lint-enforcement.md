---
title: Lint and Format Enforcement
status: active
created: 2026-04-24
---

## Vision
Enforce a single, automated style baseline (Python: ruff + black + mypy; JS: eslint + prettier) so subsequent improvement phases ship under consistent rules and review cycles stop debating style.

## Requirements
1. Python lint config in `pyproject.toml` (`[tool.ruff]`, `[tool.black]`, `[tool.mypy]`).
2. JS lint config: `.eslintrc.json` and `.prettierrc` at repo root, npm scripts in `package.json`.
3. Auto-fix existing violations and commit auto-fixes separately from manual fixes.
4. Single-entry runner: `scripts/lint.sh` that runs every linter sequentially with non-zero exit on any failure.
5. Document the toolchain in the existing `README.md` "Development" section (do not invent a new doc).

## Acceptance Criteria
- [ ] `ruff check src/kroger_mcp` exits 0
- [ ] `black --check src/kroger_mcp` exits 0
- [ ] `mypy src/kroger_mcp` exits 0 (strict=false baseline)
- [ ] `npx eslint src/kroger_mcp/web/static/js tests/playwright` exits 0
- [ ] `npx prettier --check src/kroger_mcp/web/static/js` exits 0
- [ ] `bash scripts/lint.sh` exits 0
- [ ] `pytest` still passes (no test broke from auto-fix)
- [ ] `node tests/playwright/test_all_features.js` still passes

## Technical Decisions
- **ruff selects `E,F,W,I,B,UP,SIM`** — covers errors, pyflakes, warnings, isort, bugbear, pyupgrade, simplify. Excludes nitpicky `D` (docstrings) for now.
- **Line length 100** — matches the typical Python length used in this codebase rather than enforcing 88; less churn.
- **mypy starts non-strict** — strict mode would cascade hundreds of errors across analytics/. Tighten in a later spec.
- **eslint:recommended** only — minimal rules, no opinionated style packs. Prettier handles formatting.
- **Globals declared for Alpine** — `Alpine`, `$el`, `$dispatch`, `$nextTick`, `$watch` so eslint doesn't flag them.

## Progress
- [ ] Spec approved
- [ ] Configs added
- [ ] Auto-fixes committed
- [ ] Manual fixes committed
- [ ] Verification passed
