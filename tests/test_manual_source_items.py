"""Specs for items the user buys somewhere other than Kroger.

A Kroger `product_id` is optional. An ingredient without one is not an error and
not an "override" needing justification — it is an errand: something the user
picks up at Walmart, Costco, or the Indian grocery down the road. `source` names
that vendor so the shopping list reads as a per-store errand plan instead of an
undifferentiated pile.

Two things have to hold at once, and they pull in opposite directions:

1. Unlinked items must flow freely through validation, storage, and display —
   that is the whole feature.
2. Unlinked items must NEVER reach the Kroger cart API. `check_cart_items_safety`
   is the one gate every cart-write path shares, and it previously recognized
   manual items by the synthetic `manual:<uuid>` id prefix alone. Since
   `is_manual_product_id(None)` is False, widening the write path without
   widening that predicate would have let `{"upc": None}` be POSTed to Kroger.
   The `cart_gate` tests below are the load-bearing ones.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kroger_mcp.analytics.manual_sources import (
    UNSPECIFIED_SOURCE,
    group_by_source,
    is_known_source,
    is_manual_item,
    item_source,
    manual_note,
    normalize_source,
)
from kroger_mcp.tools._cart_safety import check_cart_items_safety
from kroger_mcp.tools.recipe_tools import _validate_ingredients
from kroger_mcp.tools.shopping_list_tools import (
    _load_shopping_list,
    _save_shopping_list,
)
from kroger_mcp.tools.shopping_list_tools import register_tools as register_list_tools

USER = os.environ["KROGER_MCP_DEFAULT_USER_ID"]
LINKED_PID = "0001111015405"


# --- fixtures --------------------------------------------------------------


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point analytics DB access at a throwaway file with the full schema.

    `user_shopping_lists` is created by run_schema_migrations(), not by
    initialize_database(), so both have to run.
    """
    import kroger_mcp.analytics.database as db

    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "manual_source_items.db"))
    db.reset_initialization()
    db.initialize_database()
    db.run_schema_migrations()
    yield db
    db.reset_initialization()


# --- MCP tool plumbing -----------------------------------------------------

# The tool's declared defaults are pydantic FieldInfo objects, only resolved by a
# live MCP server. Passing the full set keeps each test's overrides honest.
_LIST_DEFAULTS: dict[str, Any] = {
    "recipe_id": None,
    "servings": None,
    "skip_items": None,
    "item_id": None,
    "item_ids": None,
    "clear_all": None,
    "quantity": None,
    "notes": None,
    "source": None,
    "modality": None,
    "confirm": None,
    "confirm_unsafe": False,
    "ctx": None,
}


def _register(register_func, tool_name: str):
    captured: dict[str, Any] = {}

    def capture_tool(func):
        captured[func.__name__] = func
        return func

    register_func(SimpleNamespace(tool=lambda: capture_tool))
    return captured[tool_name]


def _call_list_tool(action: str, **overrides) -> dict[str, Any]:
    kwargs = {**_LIST_DEFAULTS, **overrides}
    return asyncio.run(_register(register_list_tools, "shopping_list")(action=action, **kwargs))


def _install_recipe(monkeypatch, ingredients: list[dict[str, Any]]) -> str:
    """Stub the recipe lookup and the session gate so add_recipe is callable.

    `pantry(action='get_attention')` is a per-session precondition enforced in
    process memory; there is no session here, so the gate is stubbed rather than
    faked. `mcp_user_id()` is left alone — it resolves to the same default user
    the assertions read back.
    """
    import kroger_mcp.tools.recipe_tools as recipe_tools
    import kroger_mcp.tools.shopping_list_tools as list_tools

    recipe = {
        "id": "RECIPE_manual_source",
        "name": "Errand Test Recipe",
        "servings": 4,
        "ingredients": ingredients,
    }
    monkeypatch.setattr(list_tools, "_check_attention_requirement", lambda ctx: None)
    monkeypatch.setattr(recipe_tools, "_find_recipe", lambda rid: recipe)
    return recipe["id"]


# --- validation ------------------------------------------------------------


def test_validate_accepts_an_ingredient_with_only_a_name():
    """Spec: `name` is the only required ingredient field.

    This is the contract change. Previously an ingredient without a product_id
    was rejected unless it also carried override=True AND an override_reason.
    """
    assert _validate_ingredients([{"name": "masa harina"}]) == []


def test_validate_accepts_an_unlinked_ingredient_with_a_source():
    """Spec: naming the vendor is allowed but never demanded."""
    assert _validate_ingredients([{"name": "gochujang", "source": "Walmart"}]) == []


def test_validate_no_longer_requires_an_override_reason():
    """Spec: the old escape-hatch fields are inert, not mandatory.

    `override: True` with no reason used to be a validation error. It is now
    simply ignored — manual status is derived from the missing product_id.
    """
    assert _validate_ingredients([{"name": "home-grown basil", "override": True}]) == []


def test_validate_rejects_an_ingredient_without_a_name():
    """Spec: relaxing product_id did not relax everything.

    A nameless ingredient is unbuyable and undisplayable, so it still fails —
    and the error names its 1-based position so the caller can find it.
    """
    errors = _validate_ingredients([{"name": "salt"}, {"product_id": LINKED_PID}])

    assert len(errors) == 1
    assert "Ingredient 2" in errors[0]
    assert "name" in errors[0]


# --- vendor normalization --------------------------------------------------


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("walmart", "Walmart"),
        ("WALMART", "Walmart"),
        ("Wal-Mart", "Walmart"),
        ("wal mart", "Walmart"),
        ("  Walmart  ", "Walmart"),
        ("trader joes", "Trader Joe's"),
        ("Trader Joe's", "Trader Joe's"),
        ("whole foods", "Whole Foods"),
        ("sams club", "Sam's Club"),
        ("Sam's", "Sam's Club"),
        ("amazon fresh", "Amazon"),
        ("HEB", "H-E-B"),
        ("h-e-b", "H-E-B"),
    ],
)
def test_normalize_source_collapses_known_vendor_spellings(raw, canonical):
    """Spec: every spelling of a known vendor lands on one canonical name.

    Without this, "walmart" and "Wal-Mart" would open two sections of the errand
    list for what is one trip to one store.
    """
    assert normalize_source(raw) == canonical


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Indian grocery", "Indian grocery"),
        ("Fiesta Mart on Airport Blvd", "Fiesta Mart on Airport Blvd"),
        ("farmers   market", "farmers market"),
        ("  Butcher shop\n", "Butcher shop"),
    ],
)
def test_normalize_source_passes_unknown_vendors_through(raw, expected):
    """Spec: an unrecognized vendor is a supported case, never a rejection.

    Whitespace collapses (so trailing spaces don't fragment a group) but
    capitalization survives verbatim — the user's own words are the label.
    """
    assert normalize_source(raw) == expected
    assert is_known_source(raw) is False


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_normalize_source_treats_blank_as_unspecified(raw):
    """Spec: "manual, vendor unstated" is a real state with a stable label."""
    assert normalize_source(raw) == UNSPECIFIED_SOURCE


def test_normalize_source_of_an_item_reads_wire_or_column_name():
    """Spec: item_source bridges the JSON `source` / DB `manual_source` split.

    The column is `manual_source` because `user_shopping_lists.recipe_source`
    already means something else; readers should not have to care which name a
    given dict came in under.
    """
    assert item_source({"source": "wal-mart"}) == "Walmart"
    assert item_source({"manual_source": "Costco"}) == "Costco"
    assert item_source({}) == UNSPECIFIED_SOURCE


def test_normalize_note_prefers_a_vendor_but_keeps_a_legacy_reason():
    """Spec: the note never overwrites the only explanation the user wrote.

    Manual favorites predating `source` carry a free-text `override_reason`
    ("Farmers market only") and nothing else. A named vendor is more actionable
    so it wins, but with no vendor the reason must survive rather than be
    replaced by a generic line.
    """
    assert manual_note({"source": "wal-mart"}) == "Buy at Walmart"
    assert manual_note({"override_reason": "Farmers market only"}) == "Farmers market only"
    assert (
        manual_note({"source": "Costco", "override_reason": "Farmers market only"})
        == "Buy at Costco"
    )
    # Nothing to say beats saying "Buy at Manual".
    assert manual_note({}) is None
    assert manual_note({"source": "   "}) is None


# --- the manual predicate --------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"product_id": None},
        {"product_id": ""},
        {"product_id": "manual:0f2c9c3e-1a4b-4e2f-9b6a-8d1c5f7e2a10"},
    ],
)
def test_is_manual_item_covers_unlinked_and_synthetic_ids(item):
    """Spec: both routes to "cannot be ordered" resolve to one predicate."""
    assert is_manual_item(item) is True


def test_is_manual_item_leaves_linked_items_alone():
    assert is_manual_item({"product_id": LINKED_PID}) is False


# --- grouping --------------------------------------------------------------


def test_group_by_source_builds_per_vendor_sections():
    """Spec: grouping returns {source, item_count, items} and keeps item shape."""
    sections = group_by_source(
        [
            {"name": "gochujang", "source": "Walmart"},
            {"name": "rice cakes", "source": "walmart"},
            {"name": "curry leaves", "source": "Indian grocery"},
        ]
    )

    assert [s["source"] for s in sections] == ["Walmart", "Indian grocery"]
    assert [s["item_count"] for s in sections] == [2, 1]
    # The alias spelling is merged into the canonical section, not a second one.
    assert [i["name"] for i in sections[0]["items"]] == ["gochujang", "rice cakes"]
    assert sections[0]["items"][1]["source"] == "walmart", "items are not rewritten"


def test_group_by_source_orders_known_then_unknown_then_unattributed():
    """Spec: the section order is an errand order, not insertion order.

    Known vendors are real destinations, so they come first (alphabetically);
    then vendors the user named freehand; then items with no vendor at all,
    which are the least actionable and sort last.
    """
    sections = group_by_source(
        [
            {"name": "unknown-vendor item"},
            {"name": "zucchini", "source": "Indian grocery"},
            {"name": "olives", "source": "Costco"},
            {"name": "bread", "source": "Walmart"},
            {"name": "eggs", "source": "  "},
        ]
    )

    assert [s["source"] for s in sections] == [
        "Costco",
        "Walmart",
        "Indian grocery",
        UNSPECIFIED_SOURCE,
    ]
    assert sections[-1]["item_count"] == 2


def test_group_by_source_of_nothing_is_an_empty_list():
    """Spec: no manual items means no sections — not one empty section."""
    assert group_by_source([]) == []


def test_shopping_list_get_groups_manual_items_by_vendor(fresh_db, monkeypatch):
    """Spec: the shopping-list view splits Kroger from per-vendor errands.

    End-to-end through add_recipe → DB → get, because the grouping is only
    useful if `source` survives persistence. The flat
    `manual_purchase_required` list is asserted alongside the sections: it is
    preserved verbatim so existing consumers don't break.
    """
    recipe_id = _install_recipe(
        monkeypatch,
        [
            {"name": "olive oil", "quantity": 1, "unit": "bottle", "product_id": LINKED_PID},
            {"name": "gochujang", "quantity": 1, "unit": "tub", "source": "wal-mart"},
            {"name": "rice cakes", "quantity": 1, "unit": "bag", "source": "Walmart"},
            {"name": "curry leaves", "quantity": 1, "unit": "sprig", "source": "Indian grocery"},
            {"name": "sourdough starter", "quantity": 1, "unit": "jar"},
        ],
    )

    added = _call_list_tool("add_recipe", recipe_id=recipe_id, servings=4)
    assert added["success"] is True

    listed = _call_list_tool("get")

    assert listed["success"] is True
    assert listed["total_items"] == 5
    assert listed["kroger_item_count"] == 1
    assert [i["name"] for i in listed["kroger_items"]] == ["olive oil"]

    assert len(listed["manual_purchase_required"]) == 4
    sections = listed["manual_purchase_by_source"]
    assert [(s["source"], s["item_count"]) for s in sections] == [
        ("Walmart", 2),
        ("Indian grocery", 1),
        (UNSPECIFIED_SOURCE, 1),
    ]
    assert {i["name"] for i in sections[0]["items"]} == {"gochujang", "rice cakes"}


# --- the cart gate ---------------------------------------------------------


@pytest.fixture()
def filtering_off(monkeypatch):
    """Disable the safety filter — the most permissive configuration there is.

    Everything below the manual check short-circuits when filtering is off, so a
    block that still happens here is structural rather than a tunable preference.
    """
    monkeypatch.setattr("kroger_mcp.tools._cart_safety.is_filtering_enabled", lambda **_: False)


@pytest.mark.parametrize("confirm_unsafe", [False, True])
@pytest.mark.parametrize(
    "unlinked",
    [
        {"description": "sourdough starter"},
        {"product_id": None, "description": "sourdough starter"},
        {"product_id": "", "description": "sourdough starter"},
    ],
    ids=["key_absent", "none", "empty_string"],
)
def test_cart_gate_rejects_an_item_with_no_product_id(filtering_off, unlinked, confirm_unsafe):
    """Spec: an unlinked item can never be sent to Kroger.

    This is the hole the feature would otherwise have opened. The gate used to
    recognize manual items by the `manual:` id prefix alone, and
    `is_manual_product_id(None)` is False — so an item that is unlinked *because
    the user buys it at Walmart* would have been POSTed as `{"upc": None}`.

    Asserted with filtering off and with confirm_unsafe both ways, since neither
    is allowed to unlock this branch. `key_absent` also covers the old
    `item["product_id"]` subscript, which would have raised KeyError.
    """
    blocked = check_cart_items_safety(
        [{"product_id": LINKED_PID, "description": "olive oil"}, unlinked],
        user_id=USER,
        confirm_unsafe=confirm_unsafe,
    )

    assert blocked is not None, "an unlinked item passed the cart gate"
    assert blocked["success"] is False
    # A hard block, not a warning: there is no confirm-and-retry from here.
    assert blocked["requires_confirmation"] is False
    # With no id to name it by, the item is reported by its label.
    assert blocked["manual_items"] == ["sourdough starter"]
    assert "sourdough starter" in blocked["message"]


def test_cart_gate_rejects_a_synthetic_manual_id(filtering_off):
    """Spec: the pre-existing `manual:<uuid>` case still blocks.

    Manual favorites carry a synthetic id because favorite_list_items.product_id
    is NOT NULL. Widening the predicate to cover unlinked items must not have
    narrowed it away from these.
    """
    manual_pid = "manual:0f2c9c3e-1a4b-4e2f-9b6a-8d1c5f7e2a10"

    blocked = check_cart_items_safety(
        [{"product_id": manual_pid, "description": "Backyard basil"}],
        user_id=USER,
    )

    assert blocked is not None
    assert blocked["manual_items"] == [manual_pid]


def test_cart_gate_names_an_unlabelled_item_rather_than_crashing(filtering_off):
    """Spec: a manual item with neither id nor label still blocks, legibly."""
    blocked = check_cart_items_safety([{"quantity": 1}], user_id=USER)

    assert blocked is not None
    assert blocked["manual_items"] == ["(unnamed)"]


def test_cart_gate_lets_a_fully_linked_batch_through(filtering_off):
    """Spec: the gate blocks manual items, not orders.

    Without this the widened predicate could reject every batch and the failure
    would look like a working guard.
    """
    items = [
        {"product_id": LINKED_PID, "description": "olive oil"},
        {"product_id": "0001111085735", "description": "coconut milk"},
    ]

    assert check_cart_items_safety(items, user_id=USER) is None


def test_cart_gate_blocks_even_in_warn_only_mode(monkeypatch):
    """Spec: warn_only downgrades safety warnings, never the manual block.

    warn_only mode returns None for every ingredient concern. The manual check
    sits above it — and above `is_filtering_enabled` — so it is unreachable by
    configuration.
    """
    from kroger_mcp.analytics.safety import BlockMode

    monkeypatch.setattr("kroger_mcp.tools._cart_safety.is_filtering_enabled", lambda **_: True)
    monkeypatch.setattr(
        "kroger_mcp.tools._cart_safety.get_block_mode", lambda **_: BlockMode.WARN_ONLY
    )

    blocked = check_cart_items_safety(
        [{"product_id": None, "description": "sourdough starter"}],
        user_id=USER,
        confirm_unsafe=True,
    )

    assert blocked is not None
    assert blocked["requires_confirmation"] is False


def test_cart_gate_source_file_never_subscripts_product_id():
    """Spec: `product_id` is optional, so the gate must never assume the key.

    A source-level assertion because the failure mode is a KeyError on a shape
    the tests above can only sample, not exhaust.
    """
    source = Path("src/kroger_mcp/tools/_cart_safety.py").read_text()

    assert 'item["product_id"]' not in source


# --- schema migration ------------------------------------------------------


_MANUAL_SOURCE_TABLES = ("user_shopping_lists", "favorite_list_items")


def _columns(db, table: str) -> set[str]:
    conn = db.get_db_connection()
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


@pytest.mark.parametrize("table", _MANUAL_SOURCE_TABLES)
def test_migration_creates_manual_source_on_a_fresh_database(fresh_db, table):
    """Spec: a brand-new install gets the column from CREATE TABLE."""
    assert "manual_source" in _columns(fresh_db, table)


@pytest.mark.parametrize("table", _MANUAL_SOURCE_TABLES)
def test_migration_adds_manual_source_to_a_legacy_table_without_data_loss(fresh_db, table):
    """Spec: an existing DB gains the column, keeping the rows it already had.

    Simulated by dropping the column back off a fresh schema, which is the exact
    shape a pre-feature database has. The pre-existing row must still be there
    afterward with a NULL vendor — an ALTER, never a rebuild.
    """
    conn = fresh_db.get_db_connection()
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN manual_source")
        if table == "user_shopping_lists":
            conn.execute(
                "INSERT INTO user_shopping_lists (id, user_id, product_id, name, quantity) "
                "VALUES ('legacy_1', ?, ?, 'Legacy Olive Oil', 1)",
                (USER, LINKED_PID),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO favorite_lists (id, name, user_id) "
                "VALUES ('legacy_list', 'Legacy', ?)",
                (USER,),
            )
            conn.execute(
                "INSERT INTO favorite_list_items (list_id, product_id, description) "
                "VALUES ('legacy_list', ?, 'Legacy Olive Oil')",
                (LINKED_PID,),
            )
        conn.commit()
    finally:
        conn.close()

    assert "manual_source" not in _columns(fresh_db, table)

    fresh_db.run_schema_migrations()

    assert "manual_source" in _columns(fresh_db, table)

    conn = fresh_db.get_db_connection()
    try:
        rows = conn.execute(
            f"SELECT description, manual_source FROM {table}"
            if table == "favorite_list_items"
            else f"SELECT name AS description, manual_source FROM {table}"
        ).fetchall()
    finally:
        conn.close()

    assert [(r["description"], r["manual_source"]) for r in rows] == [("Legacy Olive Oil", None)]


@pytest.mark.parametrize("table", _MANUAL_SOURCE_TABLES)
def test_migration_is_idempotent(fresh_db, table):
    """Spec: re-running migrations neither raises nor duplicates the column.

    Migrations run on every startup, so a non-idempotent ALTER is a crash loop,
    not a one-time inconvenience.
    """
    fresh_db.run_schema_migrations()
    fresh_db.run_schema_migrations()

    conn = fresh_db.get_db_connection()
    try:
        names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()

    assert names.count("manual_source") == 1


def test_migration_stores_and_returns_a_vendor(fresh_db):
    """Spec: the new column is actually wired to the read/write path."""
    _save_shopping_list(
        {"items": [{"id": "mig_1", "name": "gochujang", "source": "wal-mart"}]},
        user_id=USER,
    )

    conn = fresh_db.get_db_connection()
    try:
        row = conn.execute(
            "SELECT manual_source FROM user_shopping_lists WHERE id = 'mig_1'"
        ).fetchone()
    finally:
        conn.close()

    # Normalized on the way in, so grouping never has to re-canonicalize.
    assert row["manual_source"] == "Walmart"


# --- persistence round-trip ------------------------------------------------


def test_manual_item_round_trips_its_name_through_the_db(fresh_db, monkeypatch):
    """Spec: a manual recipe ingredient keeps its name across a save/load.

    Regression: add_recipe's manual branch set only `ingredient_name`, while
    _save_shopping_list persists `name`. Manual items therefore came back from
    the database with an empty name — invisible on the very list they exist to
    appear on. The linked branch had always set both.
    """
    recipe_id = _install_recipe(
        monkeypatch,
        [{"name": "gochujang", "quantity": 2, "unit": "tbsp", "source": "Walmart"}],
    )

    _call_list_tool("add_recipe", recipe_id=recipe_id, servings=4)

    items = _load_shopping_list(user_id=USER)["items"]

    assert len(items) == 1
    assert items[0]["name"] == "gochujang"
    # Both keys are populated: readers are split between the two spellings.
    assert items[0]["ingredient_name"] == "gochujang"


def test_manual_item_round_trips_its_source_and_flag(fresh_db, monkeypatch):
    """Spec: vendor and manual status both survive persistence."""
    recipe_id = _install_recipe(
        monkeypatch,
        [
            {"name": "curry leaves", "quantity": 1, "unit": "sprig", "source": "indian grocery"},
            {"name": "olive oil", "quantity": 1, "unit": "bottle", "product_id": LINKED_PID},
        ],
    )

    _call_list_tool("add_recipe", recipe_id=recipe_id, servings=4)

    items = {item["name"]: item for item in _load_shopping_list(user_id=USER)["items"]}

    leaves = items["curry leaves"]
    assert leaves["product_id"] is None
    assert leaves["manual_purchase"] is True
    assert leaves["source"] == "indian grocery"
    assert leaves["notes"] == "Buy at indian grocery"

    oil = items["olive oil"]
    assert oil["product_id"] == LINKED_PID
    assert oil["manual_purchase"] is False
    # A Kroger item has no vendor attribution to carry.
    assert oil["source"] is None


def test_round_trip_stores_null_rather_than_the_display_sentinel(fresh_db):
    """Spec: "no vendor named" persists as NULL, not as the string "Manual".

    UNSPECIFIED_SOURCE is a display label for the catch-all section. Writing it
    to the column would make an unattributed item indistinguishable from one at
    a store actually called "Manual", and would shadow the `override_reason`
    fallback that older manual favorites depend on for their note.
    """
    _save_shopping_list(
        {
            "items": [
                {"id": "sent_1", "name": "sourdough starter"},
                {"id": "sent_2", "name": "gochujang", "source": "  "},
            ]
        },
        user_id=USER,
    )

    items = {item["name"]: item for item in _load_shopping_list(user_id=USER)["items"]}

    assert items["sourdough starter"]["source"] is None
    assert items["gochujang"]["source"] is None
    # The sentinel is still what the display layer reports for those rows.
    assert item_source(items["sourdough starter"]) == UNSPECIFIED_SOURCE


def test_round_trip_derives_manual_status_from_the_missing_product_id(fresh_db):
    """Spec: a caller cannot store an unorderable item that is unmarked.

    `manual_purchase` is derived at save time, so `manual_purchase: False` on an
    item with no product_id is corrected rather than believed. That is the bug
    class the whole "derive it, don't declare it" decision exists to kill.
    """
    _save_shopping_list(
        {
            "items": [
                {"id": "rt_1", "name": "sourdough starter", "manual_purchase": False},
                {
                    "id": "rt_2",
                    "name": "olive oil",
                    "product_id": LINKED_PID,
                    "manual_purchase": True,
                },
            ]
        },
        user_id=USER,
    )

    items = {item["name"]: item for item in _load_shopping_list(user_id=USER)["items"]}

    assert items["sourdough starter"]["manual_purchase"] is True
    assert items["olive oil"]["manual_purchase"] is False


def test_round_trip_of_update_item_assigns_a_vendor(fresh_db, monkeypatch):
    """Spec: a vendor can be attached after the fact, and only where it means something."""
    recipe_id = _install_recipe(
        monkeypatch,
        [
            {"name": "sourdough starter", "quantity": 1, "unit": "jar"},
            {"name": "olive oil", "quantity": 1, "unit": "bottle", "product_id": LINKED_PID},
        ],
    )
    _call_list_tool("add_recipe", recipe_id=recipe_id, servings=4)

    ids = {item["name"]: item["id"] for item in _load_shopping_list(user_id=USER)["items"]}

    assigned = _call_list_tool("update_item", item_id=ids["sourdough starter"], source="wal mart")
    assert assigned["success"] is True

    # Assigning a manual vendor to a Kroger-linked item is a contradiction, not
    # a preference — the two would disagree about where to buy it.
    rejected = _call_list_tool("update_item", item_id=ids["olive oil"], source="Walmart")
    assert rejected["success"] is False
    assert "Remove the product link" in rejected["error"]

    items = {item["name"]: item for item in _load_shopping_list(user_id=USER)["items"]}
    assert items["sourdough starter"]["source"] == "Walmart"
    assert items["olive oil"]["source"] is None
