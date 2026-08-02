"""One-shot, idempotent, resumable SQLite -> PostgreSQL data migrator.

Built to be RUN LATER during the real production cutover (the .108 mini), and
vetted now against a local temp SQLite -> local test Postgres. Defensive about
schema drift: it migrates only the *column intersection* between the SQLite
source and the live PG target, coerces each value to the PG column's type, and
inserts with ``ON CONFLICT DO NOTHING`` keyed on the real primary key so re-runs
are safe.

Usage:  ``SQLITE_SOURCE=/path/to.db DATABASE_URL=postgresql://localhost/db \
python scripts/etl_sqlite_to_pg.py [source_sqlite_path]``. Source: first CLI arg
or ``SQLITE_SOURCE``; target: ``DATABASE_URL``.

Exit codes: 0 = parity on every migrated table; 1 = hard failure (re-raised);
2 = parity MISMATCH on one or more tables.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field

import psycopg

# scripts/ has no __init__.py (and an unrelated `scripts` package shadows it in
# site-packages), so put this file's own directory on sys.path to make the
# sibling module importable both when run as a script and when the test suite
# loads this file by path via importlib.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pg_sequence_resync import resync_sequences as _resync_sequences  # noqa: E402

logger = logging.getLogger("kroger_mcp.etl")


# FK-dependency order: parents before children. ``users`` first (every
# user-scoped table FKs it); ``recipes``->``recipe_ingredients``,
# ``favorite_lists``->``favorite_list_items``, ``meal_plans``->``meal_entries``,
# ``meal_log``->``meal_log_items`` are parent->child pairs. Product FKs are not
# declared in the PG schema, so product-referencing tables need no ordering vs
# ``products``. Any common table absent here is appended last, sorted.
TABLE_ORDER: tuple[str, ...] = (
    "users",
    "products",
    # Product-referencing / independent (no inter-deps).
    "product_statistics",
    "price_history",
    "whole_foods_catalog",
    "deal_scan_results",
    "seasonal_patterns",
    "purchase_events",
    "pantry_items",
    "pantry_consumption_log",
    "deal_watchlist",
    # Parent -> child pairs.
    "recipes",
    "recipe_ingredients",
    "orders",
    "favorite_lists",
    "favorite_list_items",
    "meal_plans",
    "meal_entries",
    "meal_log",
    "meal_log_items",
    # User-scoped, reference only users(id).
    "safe_products",
    "blocked_products",
    "ingredient_preferences",
    "ingredient_overrides",
    "ingredient_links",
    "custom_ingredients",
    "safety_settings",
    "pending_gaps",
    "cook_deductions",
    "user_settings",
    "user_preferences",
    "user_carts",
    "user_shopping_lists",
    "user_sessions",
    "user_notion_sync",
    "kroger_tokens",
)

BATCH_SIZE = 500

_BOOL_TYPES = {"boolean"}
_UUID_TYPES = {"uuid"}
_TS_TYPES = {
    "timestamp with time zone",
    "timestamp without time zone",
    "date",
    "time without time zone",
}


@dataclass
class TableResult:
    """Outcome of migrating one table."""

    table: str
    source_count: int = 0
    inserted: int = 0
    skipped: int = 0
    orphans_dropped: int = 0  # rows whose user_id has no users row (owner gone)
    target_count: int = 0
    migrated: bool = True  # False when the whole table was skipped
    note: str = ""

    @property
    def expected_count(self) -> int:
        """Rows we expect in the target: source minus intentionally-dropped orphans."""
        return self.source_count - self.orphans_dropped

    @property
    def parity_ok(self) -> bool:
        """Parity holds when target >= expected (DO NOTHING makes re-runs safe)."""
        return self.migrated and self.target_count >= self.expected_count


@dataclass
class PgTable:
    """Introspected PG target table metadata."""

    name: str
    columns: dict[str, str] = field(default_factory=dict)  # name -> data_type
    pk_columns: list[str] = field(default_factory=list)
    defaults: dict[str, str | None] = field(default_factory=dict)  # name -> default


# ---------------------------------------------------------------------------
# Environment / connections
# ---------------------------------------------------------------------------
def _resolve_source_path(argv: list[str]) -> str:
    """Resolve the SQLite source path from argv[1] or SQLITE_SOURCE."""
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    env = os.environ.get("SQLITE_SOURCE", "").strip()
    if env:
        return env
    raise SystemExit(
        "No SQLite source given. Pass a path as the first arg or set SQLITE_SOURCE."
    )


def _resolve_target_url() -> str:
    """Resolve the PG target from DATABASE_URL."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set — cannot reach the PG target.")
    return url


def _open_sqlite(path: str) -> sqlite3.Connection:
    """Open the source SQLite DB (we never write to it)."""
    if not os.path.exists(path):
        raise SystemExit(f"SQLite source does not exist: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    """Return the set of user tables in the SQLite source."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return SQLite column names for a table (PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]  # row[1] == column name


def _pg_tables(pg: psycopg.Connection) -> set[str]:
    """Return the set of base tables in the target's public schema."""
    rows = pg.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {r[0] for r in rows}


def _introspect_pg_table(pg: psycopg.Connection, table: str) -> PgTable:
    """Introspect a PG table's columns + types and primary-key columns."""
    cols = pg.execute(
        "SELECT column_name, data_type, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    columns = {name: dtype for name, dtype, _ in cols}
    defaults = {name: dflt for name, _, dflt in cols}

    pk_rows = pg.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        (f"public.{table}",),
    ).fetchall()
    return PgTable(
        name=table,
        columns=columns,
        pk_columns=[r[0] for r in pk_rows],
        defaults=defaults,
    )


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------
def _is_empty(value: object) -> bool:
    """Return True for NULL or a blank/whitespace-only string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_valid_uuid(value: object) -> bool:
    """Return True if ``value`` parses as a UUID."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _coerce_value(value: object, pg_type: str) -> object:
    """Coerce one SQLite value to match the PG column type.

    BOOLEAN: 0/1 (or bool/str) -> bool. UUID: normalise valid strings (invalid
    ones are caught at table level first). Timestamp/date: ISO strings pass
    through, '' -> NULL. Everything else passes through.
    """
    if value is None:
        return None

    if pg_type in _BOOL_TYPES:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "t", "yes"}:
                return True
            if v in {"0", "false", "f", "no", ""}:
                return False
        return bool(value)

    if pg_type in _TS_TYPES:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    if pg_type in _UUID_TYPES:
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None  # '' can't go into a UUID column; NULL it
            if _is_valid_uuid(s):
                return str(uuid.UUID(s))
        return value

    return value


# ---------------------------------------------------------------------------
# Migration core
# ---------------------------------------------------------------------------
def _ordered_tables(common: set[str]) -> list[str]:
    """Order common tables: explicit FK order first, then the rest sorted."""
    ordered = [t for t in TABLE_ORDER if t in common]
    remaining = sorted(common - set(ordered))
    if remaining:
        logger.info("tables outside FK order, appended last: %s", ", ".join(remaining))
    return ordered + remaining


def _invalid_uuid_columns(
    rows: list[dict[str, object]], columns: list[str], pg: PgTable
) -> dict[str, int]:
    """Return uuid columns -> count of rows with a populated non-UUID value.

    NULL / empty values become NULL and are fine; only a populated non-UUID
    would corrupt the target.
    """
    uuid_cols = [c for c in columns if pg.columns.get(c) in _UUID_TYPES]
    bad: dict[str, int] = {}
    for col in uuid_cols:
        count = 0
        for row in rows:
            val = row[col]
            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue
            if not _is_valid_uuid(val):
                count += 1
        if count:
            bad[col] = count
    return bad


def _classify_user_id(
    value: object,
    default_owner: str | None,
    valid_user_ids: set[str] | None,
) -> tuple[bool, object]:
    """Decide a row's fate by its ``user_id`` (the multi-tenant FK column).

    Returns ``(keep, new_value)``:
    - NULL  -> kept as NULL (an intentionally-unscoped row).
    - a valid UUID that exists in ``valid_user_ids`` -> kept, canonicalised.
    - an ORPHAN (valid UUID with no ``users`` row — owner deleted or pre-accounts)
      or a non-UUID sentinel (e.g. ``'default'``) -> reassigned to ``default_owner``
      if one is configured (opt-in via ``KROGER_MCP_DEFAULT_USER_ID``), otherwise
      DROPPED (``keep=False``). Dropping is the safe default: the owner no longer
      exists, so an FK-enforcing database would already have cascade-deleted it.

    When ``valid_user_ids`` is None the orphan check is disabled (any valid UUID
    is kept) — used when the source has no ``users`` table to validate against.
    """
    if value is None:
        return True, None
    s = str(value).strip()
    if _is_valid_uuid(s):
        canonical = str(uuid.UUID(s))
        if valid_user_ids is None or canonical in valid_user_ids:
            return True, canonical
        # orphan: valid UUID, no matching users row
    # orphan or non-UUID sentinel
    if default_owner is not None:
        return True, default_owner
    return False, None


def _migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg: psycopg.Connection,
    table: str,
    default_owner: str | None = None,
    valid_user_ids: set[str] | None = None,
) -> TableResult:
    """Migrate one table's column-intersection rows into PG."""
    result = TableResult(table=table)
    pg_meta = _introspect_pg_table(pg, table)
    sqlite_cols = _sqlite_columns(sqlite_conn, table)

    common_cols = [c for c in sqlite_cols if c in pg_meta.columns]
    if not common_cols:
        result.migrated = False
        result.note = "no shared columns"
        logger.warning("[%s] no shared columns — skipped", table)
        return result

    src_only = sorted(set(sqlite_cols) - set(pg_meta.columns))
    pg_only = sorted(set(pg_meta.columns) - set(sqlite_cols))
    if src_only:
        logger.info("[%s] source-only columns dropped: %s", table, ", ".join(src_only))
    if pg_only:
        logger.info(
            "[%s] target-only columns -> PG default/NULL: %s", table, ", ".join(pg_only)
        )

    select_cols = ", ".join(f'"{c}"' for c in common_cols)
    rows = sqlite_conn.execute(f'SELECT {select_cols} FROM "{table}"').fetchall()
    result.source_count = len(rows)

    if result.source_count == 0:
        logger.info("[%s] source has 0 rows — nothing to do", table)
        result.target_count = _pg_count(pg, table)
        return result

    # Materialise rows as dicts so user_id can be normalised in place before the
    # invalid-UUID check + coercion (genuine NULL stays NULL; '' / legacy
    # sentinels -> default owner). Other uuid columns (e.g. users.id) keep the
    # skip-the-table backstop below.
    dict_rows = [dict(r) for r in rows]
    if "user_id" in common_cols and pg_meta.columns.get("user_id") in _UUID_TYPES:
        kept_rows: list[dict[str, object]] = []
        for r in dict_rows:
            keep, new_uid = _classify_user_id(
                r.get("user_id"), default_owner, valid_user_ids
            )
            if keep:
                r["user_id"] = new_uid
                kept_rows.append(r)
            else:
                result.orphans_dropped += 1
        dict_rows = kept_rows
        if result.orphans_dropped:
            logger.warning(
                "[%s] dropped %d orphan row(s): user_id has no users row and no "
                "default owner set (KROGER_MCP_DEFAULT_USER_ID) to reassign to.",
                table,
                result.orphans_dropped,
            )

    bad_uuids = _invalid_uuid_columns(dict_rows, common_cols, pg_meta)
    if bad_uuids:
        detail = ", ".join(f"{c}={n}" for c, n in bad_uuids.items())
        result.migrated = False
        result.skipped = result.source_count
        result.note = f"invalid UUID values ({detail})"
        result.target_count = _pg_count(pg, table)
        logger.warning(
            "[%s] SKIPPED entire table — non-UUID values in uuid column(s): %s. "
            "Fix the source before cutover.",
            table,
            detail,
        )
        return result

    # Omit columns the source left entirely empty when PG can supply a DEFAULT
    # (e.g. user_sessions.id UUID DEFAULT gen_random_uuid()): inserting NULL would
    # violate NOT NULL, and an all-empty column can't be referenced, so letting
    # the default fire is safe.
    insert_cols = list(common_cols)
    if dict_rows:
        omitted = [
            c
            for c in insert_cols
            if pg_meta.defaults.get(c) and all(_is_empty(r.get(c)) for r in dict_rows)
        ]
        if omitted:
            logger.info(
                "[%s] empty in source -> PG default fills (column omitted): %s",
                table,
                ", ".join(omitted),
            )
            insert_cols = [c for c in insert_cols if c not in omitted]

    col_types = [pg_meta.columns[c] for c in insert_cols]
    payload: list[tuple[object, ...]] = [
        tuple(
            _coerce_value(row[c], t)
            for c, t in zip(insert_cols, col_types, strict=True)
        )
        for row in dict_rows
    ]

    result.inserted = _insert_rows(pg, table, insert_cols, pg_meta.pk_columns, payload)
    # skipped = kept rows that conflicted with an existing target row (re-runs);
    # dropped orphans are tracked separately and excluded from the expected count.
    result.skipped = len(dict_rows) - result.inserted
    result.target_count = _pg_count(pg, table)

    logger.info(
        "[%s] source=%d inserted=%d skipped(existing)=%d orphans_dropped=%d target=%d",
        table,
        result.source_count,
        result.inserted,
        result.skipped,
        result.orphans_dropped,
        result.target_count,
    )
    return result


def _insert_rows(
    pg: psycopg.Connection,
    table: str,
    columns: list[str],
    pk_columns: list[str],
    payload: list[tuple[object, ...]],
) -> int:
    """Batch-insert rows with ON CONFLICT DO NOTHING; return inserted count."""
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))

    if pk_columns:
        target = ", ".join(f'"{c}"' for c in pk_columns)
        conflict = f" ON CONFLICT ({target}) DO NOTHING"
    else:
        conflict = ""
        logger.warning(
            "[%s] no primary key — inserting WITHOUT ON CONFLICT; re-runs may "
            "duplicate rows",
            table,
        )

    sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders}){conflict}'
    inserted = 0
    with pg.cursor() as cur:
        for start in range(0, len(payload), BATCH_SIZE):
            chunk = payload[start : start + BATCH_SIZE]
            cur.executemany(sql, chunk)
            inserted += _affected(cur, len(chunk))
    pg.commit()
    return inserted


def _affected(cur: psycopg.Cursor, requested: int) -> int:
    """Best-effort affected-row count for the last executemany batch.

    When the server reports no count (rowcount < 0) we assume all requested rows
    inserted; the end-of-run parity check is the real safety net.
    """
    rc = cur.rowcount
    if rc is None or rc < 0:
        return requested
    return min(rc, requested)


def _pg_count(pg: psycopg.Connection, table: str) -> int:
    """Return the live row count of a PG table."""
    row = pg.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_etl(source_path: str, database_url: str) -> int:
    """Run the full migration. Returns a process exit code."""
    logger.info("ETL start: source=%s target=%s", source_path, _redact(database_url))

    default_owner = os.environ.get("KROGER_MCP_DEFAULT_USER_ID", "").strip() or None
    if default_owner and not _is_valid_uuid(default_owner):
        raise SystemExit(
            f"KROGER_MCP_DEFAULT_USER_ID is not a valid UUID: {default_owner!r}"
        )
    if default_owner:
        logger.info("default owner for stray/empty user_id values: %s", default_owner)

    _initialize_pg_schema(database_url)

    sqlite_conn = _open_sqlite(source_path)
    pg = psycopg.connect(database_url)
    try:
        src_tables = _sqlite_tables(sqlite_conn)
        tgt_tables = _pg_tables(pg)
        common = src_tables & tgt_tables

        source_only = sorted(src_tables - tgt_tables)
        target_only = sorted(tgt_tables - src_tables)
        if source_only:
            # A source table with DATA that has no PG target would be silently
            # dropped — refuse outright. Empty source-only tables are harmless
            # (legacy/ephemeral) and only warned about.
            dropped_with_data = []
            for t in source_only:
                n = sqlite_conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if n:
                    dropped_with_data.append((t, int(n)))
            if dropped_with_data:
                detail = ", ".join(f"{t}({n} rows)" for t, n in dropped_with_data)
                raise SystemExit(
                    "Refusing to migrate: source table(s) with data are absent "
                    f"from the PG schema and would be DROPPED: {detail}. Add them "
                    "to kroger_mcp.analytics.pg_database.SCHEMA_SQL first."
                )
            logger.warning(
                "empty tables in SQLite but NOT in PG (skipped, no data lost): %s",
                ", ".join(source_only),
            )
        if target_only:
            logger.info(
                "tables in PG but NOT in SQLite (stay empty): %s",
                ", ".join(target_only),
            )

        # The set of real user ids (canonical UUIDs) that the users table will
        # provide. Rows whose user_id is a valid UUID but absent from this set
        # are orphans (deleted/legacy owner) and get reassigned to default_owner
        # so the PG foreign key holds. None disables the orphan check.
        valid_user_ids: set[str] | None = None
        if "users" in src_tables:
            valid_user_ids = {
                str(uuid.UUID(str(r[0])))
                for r in sqlite_conn.execute("SELECT id FROM users").fetchall()
                if r[0] and _is_valid_uuid(str(r[0]))
            }
            logger.info("source has %d real user id(s)", len(valid_user_ids))
            if default_owner and default_owner not in valid_user_ids:
                logger.warning(
                    "default owner %s is not among the source users — orphan "
                    "rows reassigned to it would violate the FK",
                    default_owner,
                )

        ordered = _ordered_tables(common)
        logger.info("migrating %d tables: %s", len(ordered), ", ".join(ordered))

        results: list[TableResult] = []
        for table in ordered:
            try:
                results.append(
                    _migrate_table(
                        sqlite_conn, pg, table, default_owner, valid_user_ids
                    )
                )
            except Exception:
                pg.rollback()
                logger.error("[%s] migration FAILED — rolling back this table", table)
                raise

        # Rows arrive carrying their source ids, which never touches nextval() —
        # so every sequence is still parked below its table's max and would
        # collide on the app's next insert. Must run after ALL tables land.
        _resync_sequences(pg)

        return _report(results)
    finally:
        sqlite_conn.close()
        pg.close()


def _initialize_pg_schema(database_url: str) -> None:
    """Create the PG schema via the app's own initializer.

    ``initialize_pg_database`` uses a module-level connection pool keyed on
    ``DATABASE_URL`` at first use. We reset it first so a stale pool bound to a
    different DSN (e.g. across test runs) cannot leak into this migration.
    """
    os.environ["DATABASE_URL"] = database_url
    from kroger_mcp.analytics.pg_database import (
        close_pool,
        initialize_pg_database,
    )

    close_pool()
    logger.info("initializing PG schema (initialize_pg_database)")
    initialize_pg_database()


def _report(results: list[TableResult]) -> int:
    """Print a PASS/MISMATCH parity summary; return exit code."""
    logger.info("=" * 78)
    logger.info("PARITY SUMMARY")
    hdr = "%-24s %7s %7s %8s %7s %8s  %s"
    logger.info(hdr, "table", "source", "ins", "orphdrop", "target", "status", "note")
    logger.info("-" * 78)

    mismatches = 0
    skipped_tables = 0
    total_orphans = 0
    for r in sorted(results, key=lambda x: x.table):
        total_orphans += r.orphans_dropped
        if not r.migrated:
            status = "SKIP"
            skipped_tables += 1
        elif r.parity_ok:
            status = "PASS"
        else:
            status = "MISMATCH"
            mismatches += 1
        logger.info(
            hdr,
            r.table,
            r.source_count,
            r.inserted,
            r.orphans_dropped,
            r.target_count,
            status,
            r.note,
        )

    logger.info("-" * 78)
    logger.info(
        "tables=%d  pass=%d  mismatch=%d  skipped=%d  orphan_rows_dropped=%d",
        len(results),
        len(results) - mismatches - skipped_tables,
        mismatches,
        skipped_tables,
        total_orphans,
    )
    if total_orphans:
        logger.warning(
            "%d row(s) dropped because their user_id has no users row (deleted/"
            "pre-account owner). Set KROGER_MCP_DEFAULT_USER_ID to reassign instead.",
            total_orphans,
        )

    if mismatches:
        logger.error("PARITY MISMATCH on %d table(s) — see summary", mismatches)
        return 2
    if skipped_tables:
        logger.warning(
            "%d table(s) skipped (see notes). Review before the real cutover.",
            skipped_tables,
        )
    logger.info("ALL MIGRATED TABLES AT PARITY")
    return 0


def _redact(url: str) -> str:
    """Redact any password in a DSN before logging."""
    if "@" in url and "//" in url:
        scheme, _, rest = url.partition("//")
        creds, _, host = rest.partition("@")
        if ":" in creds:
            user = creds.split(":", 1)[0]
            return f"{scheme}//{user}:***@{host}"
    return url


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = list(sys.argv if argv is None else argv)
    return run_etl(_resolve_source_path(args), _resolve_target_url())


if __name__ == "__main__":
    sys.exit(main())
