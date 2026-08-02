"""Advance Postgres sequences past the max value of the column they own.

Owned by the SQLite -> Postgres migration path (``scripts/etl_sqlite_to_pg.py``
calls :func:`resync_sequences` at the end of every run), and runnable standalone
to repair a database whose sequences have already drifted.

WHY THIS EXISTS: the ETL migrates rows carrying their *source* ``id`` values,
which bypasses ``nextval()`` entirely. Each sequence is therefore left wherever
the freshly-created schema put it — far below the table's real max — so every
subsequent app insert collides with a migrated row and raises ``UniqueViolation``
until the counter grinds past that max. This is not hypothetical: it silently
took out production price-history recording for weeks (1390
``price_history_pkey`` violations in the scanner log) before being caught during
a routine health check on 2026-08-02.

Usage:  ``DATABASE_URL=postgresql://localhost/db python scripts/pg_sequence_resync.py``

Exit codes: 0 = success (whether or not anything needed advancing); 1 = failure.
"""

from __future__ import annotations

import logging
import os
import sys

import psycopg

logger = logging.getLogger("kroger_mcp.etl.sequences")

# Sequences owned by a table column (``deptype = 'a'`` — auto-dependency, what
# SERIAL / GENERATED ... AS IDENTITY create). Discovering them through the
# catalog rather than a hardcoded list means tables added to the schema later
# are covered with no edit here.
_OWNED_SEQUENCES_SQL = """
SELECT c.oid, c.relname, t.relname, a.attname
FROM pg_class c
JOIN pg_depend d ON d.objid = c.oid
 AND d.classid = 'pg_class'::regclass AND d.deptype = 'a'
JOIN pg_class t ON t.oid = d.refobjid
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
WHERE c.relkind = 'S'
ORDER BY t.relname
"""


def resync_sequences(pg: psycopg.Connection) -> int:
    """Advance every owned sequence to its column's max value; return how many moved.

    A sequence is never moved BACKWARD. One already ahead of the max is harmless
    (it only leaves gaps in the id space), whereas lowering it could collide with
    a value an in-flight transaction has already claimed via ``nextval()``.

    Commits on success. Raises ``psycopg.Error`` if the catalog query or any
    ``setval`` fails — callers should let that abort the migration rather than
    leaving sequences half-repaired.
    """
    owned = pg.execute(_OWNED_SEQUENCES_SQL).fetchall()

    advanced = 0
    for seq_oid, seq_name, table, column in owned:
        try:
            row = pg.execute(f'SELECT COALESCE(MAX("{column}"), 0) FROM "{table}"').fetchone()
            max_id = int(row[0]) if row else 0

            current = pg.execute(
                "SELECT COALESCE(pg_sequence_last_value(%s), 0)", (seq_oid,)
            ).fetchone()
            last_value = int(current[0]) if current else 0

            if max_id <= last_value:
                logger.debug(
                    "[%s] sequence %s already at %d (max %d) — left alone",
                    table,
                    seq_name,
                    last_value,
                    max_id,
                )
                continue

            pg.execute("SELECT setval(%s, %s, true)", (seq_oid, max_id))
            advanced += 1
            logger.info(
                "[%s] sequence %s advanced %d -> %d (max of %s.%s)",
                table,
                seq_name,
                last_value,
                max_id,
                table,
                column,
            )
        except psycopg.Error:
            logger.error(
                "[%s] FAILED to resync sequence %s on column %s.%s",
                table,
                seq_name,
                table,
                column,
                exc_info=True,
            )
            pg.rollback()
            raise

    pg.commit()
    logger.info("sequence resync: %d of %d sequence(s) advanced", advanced, len(owned))
    return advanced


def main(argv: list[str] | None = None) -> int:
    """Repair sequence drift on the database named by ``DATABASE_URL``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return 1

    with psycopg.connect(database_url) as pg:
        advanced = resync_sequences(pg)
    logger.info("done — %d sequence(s) needed advancing", advanced)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
