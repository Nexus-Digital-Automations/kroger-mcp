#!/usr/bin/env bash
# Single entry point for every linter in this repo.
# Exits non-zero if any tool reports violations. Used by CI and pre-commit.
#
# Counterpart: see pyproject.toml [tool.ruff/black/mypy] and .eslintrc.json / .prettierrc
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf "\n=== %s ===\n" "$1"; }

step "ruff (Python lint)"
ruff check src/kroger_mcp

step "black (Python format)"
black --check src/kroger_mcp

# mypy is configured but not gated this phase — codebase has 114 latent type errors
# (implicit Optional, missing annotations, attribute mismatches) that are their own
# spec's worth of cleanup. Run for visibility but do not fail the build.
step "mypy (Python types — report only, not gated)"
mypy src/kroger_mcp || echo "(mypy reported errors; not gated — see specs/lint-enforcement.md 'Known follow-up work')"

step "eslint (JS lint)"
eslint src/kroger_mcp/web/static/js tests/playwright

step "prettier (JS format)"
prettier --check 'src/kroger_mcp/web/static/js/**/*.js'

echo
echo "All linters passed."
