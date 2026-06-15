#!/bin/bash
# Run the full test suite against a throwaway local PostgreSQL database (AC-2).
#
# Mirror of the default SQLite test run, but with DATABASE_URL pointed at a
# disposable PG database. Per-test isolation comes from the _pg_isolation_between_tests
# fixture in tests/conftest.py (TRUNCATE + reseed before each test), which gives
# each test the fresh-DB semantics the SQLite suite gets from a per-test tmp file.
#
# Requires a local Postgres accepting connections (see scripts/provision_prod.sh
# for the prod install; locally any postgres on :5432 works). $0, no external calls.
#
# Usage: scripts/test_on_pg.sh [pytest args...]   (default: tests/)
set -euo pipefail
cd "$(dirname "$0")/.."

ADMIN_DSN="${ETL_TEST_PG_ADMIN:-postgresql://localhost:5432/postgres}"
DBNAME="smartshopper_pgtest_$$"

cleanup() {
  psql "$ADMIN_DSN" -tAc "DROP DATABASE IF EXISTS $DBNAME WITH (FORCE);" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[test_on_pg] creating throwaway db $DBNAME"
psql "$ADMIN_DSN" -tAc "CREATE DATABASE $DBNAME;" >/dev/null

export DATABASE_URL="postgresql://localhost:5432/$DBNAME"
echo "[test_on_pg] DATABASE_URL=$DATABASE_URL"
python -c "import kroger_mcp.analytics.database as db; db.ensure_initialized()" >/dev/null 2>&1

PYTEST_ARGS=("${@:-tests/}")
python -m pytest "${PYTEST_ARGS[@]}" -p no:cacheprovider
