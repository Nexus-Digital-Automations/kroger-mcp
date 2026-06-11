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

logger = logging.getLogger("kroger_mcp.etl")


# FK-dependency order: parents before children. Common tables absent here are
# appended last, sorted, best-effort.
TABLE_ORDER: tuple[str, ...] = (
    "users",
    "products",
    "recipes",
    "recipe_ingredients",
    "orders",
    "purchase_events",
    "pantry_items",
    "favorite_lists",
    "favorite_list_items",
    "meal_plans",
    "meal_entries",
    "safe_products",
    "blocked_products",
    "price_history",
    "ingredient_links",
    "user_settings",
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
    target_count: int = 0
    migrated: bool = True  # False when the whole table was skipped
    note: str = ""

    @property
    def parity_ok(self) -> bool:
        """Parity holds when target >= source (DO NOTHING makes re-runs safe)."""
        return self.migrated and self.target_count >= self.source_count


@dataclass
class PgTable:
    """Introspected PG target table metadata."""

    name: str
    columns: dict[str, str] = field(default_factory=dict)  # name -> data_type
    pk_columns: list[str] = field(default_factory=list)


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
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    columns = {name: dtype for name, dtype in cols}

    pk_rows = pg.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        (f"public.{table}",),
    ).fetchall()
    return PgTable(name=table, columns=columns, pk_columns=[r[0] for r in pk_rows])


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------
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
        if isinstance(value, str) and _is_valid_uuid(value):
            return str(uuid.UUID(value))
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
    rows: list[sqlite3.Row], columns: list[str], pg: PgTable
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


def _migrate_table(
    sqlite_conn: sqlite3.Connection, pg: psycopg.Connection, table: str
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

    bad_uuids = _invalid_uuid_columns(rows, common_cols, pg_meta)
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

    col_types = [pg_meta.columns[c] for c in common_cols]
    payload: list[tuple[object, ...]] = [
        tuple(
            _coerce_value(row[c], t)
            for c, t in zip(common_cols, col_types, strict=True)
        )
        for row in rows
    ]

    result.inserted = _insert_rows(pg, table, common_cols, pg_meta.pk_columns, payload)
    result.skipped = result.source_count - result.inserted
    result.target_count = _pg_count(pg, table)

    logger.info(
        "[%s] source=%d inserted=%d skipped(existing)=%d target=%d",
        table,
        result.source_count,
        result.inserted,
        result.skipped,
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
            logger.warning(
                "tables in SQLite but NOT in PG (not migrated): %s",
                ", ".join(source_only),
            )
        if target_only:
            logger.info(
                "tables in PG but NOT in SQLite (stay empty): %s",
                ", ".join(target_only),
            )

        ordered = _ordered_tables(common)
        logger.info("migrating %d tables: %s", len(ordered), ", ".join(ordered))

        results: list[TableResult] = []
        for table in ordered:
            try:
                results.append(_migrate_table(sqlite_conn, pg, table))
            except Exception:
                pg.rollback()
                logger.error("[%s] migration FAILED — rolling back this table", table)
                raise

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
    logger.info("=" * 72)
    logger.info("PARITY SUMMARY")
    hdr = "%-26s %8s %8s %8s %8s  %s"
    logger.info(hdr, "table", "source", "inserted", "target", "status", "note")
    logger.info("-" * 72)

    mismatches = 0
    skipped_tables = 0
    for r in sorted(results, key=lambda x: x.table):
        if not r.migrated:
            status = "SKIP"
            skipped_tables += 1
        elif r.parity_ok:
            status = "PASS"
        else:
            status = "MISMATCH"
            mismatches += 1
        logger.info(
            hdr, r.table, r.source_count, r.inserted, r.target_count, status, r.note
        )

    logger.info("-" * 72)
    logger.info(
        "tables=%d  pass=%d  mismatch=%d  skipped=%d",
        len(results),
        len(results) - mismatches - skipped_tables,
        mismatches,
        skipped_tables,
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
