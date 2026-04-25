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

step "mypy (Python types)"
mypy src/kroger_mcp

step "eslint (JS lint)"
eslint src/kroger_mcp/web/static/js tests/playwright

step "prettier (JS format)"
prettier --check 'src/kroger_mcp/web/static/js/**/*.js'

echo
echo "All linters passed."
