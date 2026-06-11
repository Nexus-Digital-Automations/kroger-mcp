"""Unit tests for the SQLite->Postgres ETL hardening (no Postgres required).

Covers the pure transforms added for the production cutover:
- user_id normalisation (genuine NULL preserved; '' / sentinels -> default owner;
  valid UUIDs canonicalised),
- UUID coercion of empty strings to NULL,
- TABLE_ORDER integrity (FK-ordered parent->child, no duplicates).
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

_ETL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "etl_sqlite_to_pg.py"
_spec = importlib.util.spec_from_file_location("etl_sqlite_to_pg", _ETL_PATH)
assert _spec and _spec.loader
etl = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass introspection (which reads sys.modules) works.
sys.modules["etl_sqlite_to_pg"] = etl
_spec.loader.exec_module(etl)

_OWNER = "0a5bb6de-df4b-4ff9-8c98-01dc8f3c4950"


_VALID = {_OWNER, "a7fd322a-e4de-4067-b3aa-2721ca684d3c"}


def test_classify_null_is_kept_as_null():
    assert etl._classify_user_id(None, _OWNER, _VALID) == (True, None)


def test_classify_valid_in_users_is_kept_canonical():
    raw = "0A5BB6DE-DF4B-4FF9-8C98-01DC8F3C4950"
    keep, val = etl._classify_user_id(raw, None, _VALID)
    assert keep and val == str(uuid.UUID(raw))


def test_classify_orphan_dropped_without_owner():
    orphan = "27bf9a89-dcab-5b28-a6a6-108a51f5e95a"  # valid UUID, not in users
    assert etl._classify_user_id(orphan, None, _VALID) == (False, None)


def test_classify_orphan_remapped_with_owner():
    orphan = "27bf9a89-dcab-5b28-a6a6-108a51f5e95a"
    assert etl._classify_user_id(orphan, _OWNER, _VALID) == (True, _OWNER)


def test_classify_sentinel_dropped_without_owner():
    assert etl._classify_user_id("default", None, _VALID) == (False, None)
    assert etl._classify_user_id("", None, _VALID) == (False, None)


def test_classify_disabled_when_no_valid_set():
    # valid_user_ids=None disables the orphan check -> any valid UUID kept.
    orphan = "27bf9a89-dcab-5b28-a6a6-108a51f5e95a"
    keep, val = etl._classify_user_id(orphan, None, None)
    assert keep and val == orphan


def test_coerce_empty_string_uuid_to_none():
    assert etl._coerce_value("", "uuid") is None
    assert etl._coerce_value("   ", "uuid") is None


def test_coerce_valid_uuid_canonicalises():
    raw = "0A5BB6DE-DF4B-4FF9-8C98-01DC8F3C4950"
    assert etl._coerce_value(raw, "uuid") == str(uuid.UUID(raw))


def test_table_order_has_no_duplicates():
    assert len(etl.TABLE_ORDER) == len(set(etl.TABLE_ORDER))


def test_table_order_respects_parent_child():
    order = etl.TABLE_ORDER
    pairs = [
        ("users", "kroger_tokens"),
        ("recipes", "recipe_ingredients"),
        ("favorite_lists", "favorite_list_items"),
        ("meal_plans", "meal_entries"),
        ("meal_log", "meal_log_items"),
    ]
    for parent, child in pairs:
        assert parent in order and child in order
        assert order.index(parent) < order.index(child), f"{parent} must precede {child}"
