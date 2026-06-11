# Spec — Full PostgreSQL port (backend-aware analytics layer)

## Context
`get_db_connection()`/`get_db_cursor()` in `analytics/database.py` are SQLite-only
(`sqlite3.connect(DB_FILE)`, `?` placeholders). ~40 files in analytics/tools/web use
them directly, so only auth/sessions/tokens currently run on Postgres; all real data
stays in SQLite even when `DATABASE_URL` is set. The user chose a full port so the app
truly runs on Postgres (required for the multi-machine / write-concurrency goal).

## Approach
A **backend-aware connection shim** minimizes churn: when `get_backend()=="postgresql"`,
`get_db_connection()`/`get_db_cursor()` return a psycopg-backed adapter that:
- translates `?`→`%s` and escapes literal `%`→`%%` (psycopg paramstyle),
- translates `INSERT OR IGNORE` → `... ON CONFLICT DO NOTHING`,
- yields **hybrid rows** supporting both `row[0]` and `row["col"]` (sqlite3.Row parity),
- supports `.execute/.executemany/.commit/.close/.fetchone/.fetchall` and a settable
  `row_factory` no-op, and ignores `PRAGMA` statements,
- returns the pooled connection on `.close()`.
SQLite path is unchanged. DDL stays per-backend (SQLite `initialize_database` vs PG
`SCHEMA_SQL`) — already the case.

Manual per-site fixes (the shim can't infer these):
- `INSERT OR REPLACE` (4) → `INSERT ... ON CONFLICT (<pk>) DO UPDATE SET …`.
- `lastrowid` (5) → `RETURNING id` + read it back.
- `strftime(...)` (28) → PG equivalents (`to_char`, `date_trunc`, `EXTRACT`), preserving
  the exact bucketing semantics (month/week/day/hour) the analytics rely on.

## Acceptance criteria (testable)
- AC-1 Shim: a unit test proves `?`/`%`/`INSERT OR IGNORE` translation, hybrid row access
  (index AND name), commit/close, and pool return — against a live local Postgres.
- AC-2 **The FULL existing test suite passes with `DATABASE_URL` set to a Postgres DB**
  (a new CI-style run: same tests, PG backend), in addition to the SQLite run staying green.
- AC-3 No remaining SQLite-only SQL on the data path: zero `strftime`, `INSERT OR
  REPLACE`, `lastrowid`, `INSERT OR IGNORE` reachable on the PG backend (shim-translated
  or rewritten). `PRAGMA` only in the SQLite connection-setup path.
- AC-4 A smoke test exercising representative features on PG (pantry write+read, recipe
  save, safety scan, a prediction/seasonal query that used strftime, consent) returns
  correct results.
- AC-5 mypy 0, ruff 0 on all changed files; SQLite suite remains green (parity, not regression).

## Execution order
1. Build the shim in `database.py` (+ unit test) — measure suite-on-PG pass rate.
2. Fix the failures surfaced by the PG suite run, grouped by file/area (fleet): strftime
   rewrites, INSERT OR REPLACE, lastrowid.
3. Add a pytest mechanism to run the suite against PG (env-driven DATABASE_URL + a session
   fixture that initializes the PG schema), so AC-2 is repeatable.
4. Iterate until the PG suite is green; keep the SQLite suite green throughout.

## Risk notes (from the audit)
- 28 `strftime` sites are the real work; semantics must match exactly (off-by-one in date
  bucketing silently corrupts predictions/seasonality).
- ETL `TABLE_ORDER` must include all migratable tables before the real data migration
  (custom_ingredients/ingredient_overrides/pending_gaps/cook_deductions not yet listed).
- `seasonal_patterns` global-vs-user-scoped drift to reconcile.
- Boolean columns: SQLite stores 0/1; PG is strict — reads come back as bool, ensure call
  sites that compare `== 1` still work (hybrid row returns the PG-native bool).
