"""Regression coverage for run_schema_migrations()'s foreign-key safety.

_rebuild_table_add_user_id (database.py) rebuilds a formerly-global SQLite
table (e.g. favorite_lists) into a user-scoped shape via CREATE-new / copy /
DROP-old / RENAME. Every connection enables `PRAGMA foreign_keys = ON`
(get_db_connection), and SQLite's DROP TABLE performs an implicit
`DELETE FROM` the dropped table first when FK enforcement is on -- which
fires `ON DELETE CASCADE` against dependents (favorite_list_items on
favorite_lists) even though only the parent's shape was meant to change.
That silently destroyed real user data on any fresh/unmigrated install with
real favorites. Fixed by disabling foreign_keys for the whole
run_schema_migrations() transaction (must happen before BEGIN -- the pragma
is a no-op mid-transaction) and re-enabling it after commit, plus a
post-rebuild integrity check scoped to the rebuilt table's actual dependents
(not a whole-database check, which would also trip on unrelated pre-existing
FK debt elsewhere in a real, long-lived dev/prod DB).

Uses an isolated tmp_path DB throughout -- never touches the real dev DB.
"""

from __future__ import annotations

from pathlib import Path


def _fresh_db(tmp_path: Path, monkeypatch, name: str):
    import kroger_mcp.analytics.database as db

    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / name))
    return db


def test_rebuild_does_not_cascade_wipe_dependent_rows(tmp_path, monkeypatch):
    """The exact scenario that caused data loss: a real favorite_lists row with
    a real favorite_list_items row, on a schema that has never run
    run_schema_migrations() (and so has no user_id column yet) -- the
    rebuild path this test exercises."""
    db = _fresh_db(tmp_path, monkeypatch, "fk_safety_cascade.db")
    db.initialize_database()

    conn = db.get_db_connection()
    try:
        conn.execute(
            "INSERT INTO favorite_lists (id, name, description, list_type) "
            "VALUES ('fk_safety_list', 'FK Safety Test', '', 'custom')"
        )
        conn.execute(
            "INSERT INTO favorite_list_items "
            "(list_id, product_id, description, brand, default_quantity) "
            "VALUES ('fk_safety_list', 'FK_SAFETY_PID', 'Test Item', 'Brand', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    db.run_schema_migrations()

    conn = db.get_db_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM favorite_list_items WHERE list_id = 'fk_safety_list'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1, (
        "favorite_list_items row was wiped by the favorite_lists rebuild -- "
        "the DROP TABLE cascade-on-FK-enforced-connection bug regressed"
    )


def test_rebuild_leaves_foreign_keys_enabled_afterward(tmp_path, monkeypatch):
    """foreign_keys is disabled only for the migration transaction itself --
    it must be back on by the time run_schema_migrations() returns, or every
    later write in the process loses FK enforcement."""
    db = _fresh_db(tmp_path, monkeypatch, "fk_safety_reenable.db")
    db.initialize_database()
    db.run_schema_migrations()

    conn = db.get_db_connection()
    try:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()

    assert enabled == 1


def test_unrelated_preexisting_fk_debt_does_not_block_migration(tmp_path, monkeypatch):
    """The scoped post-rebuild check must only inspect the rebuilt table's own
    dependents -- NOT the whole database. A dangling reference in a table the
    migration never touches (e.g. price_history -> products, which can and
    does accumulate real drift over a long-lived dev/prod DB) must not raise."""
    db = _fresh_db(tmp_path, monkeypatch, "fk_safety_unrelated_debt.db")
    db.initialize_database()

    conn = db.get_db_connection()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO price_history "
            "(product_id, regular_price, on_sale, location_id, observed_at, source) "
            "VALUES ('NONEXISTENT_PRODUCT', 1.0, 0, '00000000', '2026-01-01T00:00:00', 'test')"
        )
        conn.commit()
    finally:
        conn.close()

    db.run_schema_migrations()  # must not raise despite the orphan row above
