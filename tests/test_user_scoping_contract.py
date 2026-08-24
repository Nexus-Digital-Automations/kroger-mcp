"""Regression guards for the multi-tenant user_id scoping audits.

Two distinct shapes of the same cross-tenant leak are guarded here.

SHAPE 1 (2026-07 audit) -- signature level: a function takes `user_id` but
treats a missing value as fine, silently resolving it to a shared default
owner instead of making the caller supply the real one.

SHAPE 2 (2026-08 audit) -- query level: the function takes `user_id`
correctly, but a SQL `LEFT JOIN` onto a per-user table matches on a shared
key alone (e.g. `ON a.product_id = pi.product_id`), so it can pull in a
DIFFERENT tenant's row. Signature-level checks cannot see this. It was found
in five `favorites.py` sites, `prediction_tools.py`, and `categories.py`,
where `predictions.get_by_category` returned another user's purchase history
straight to the caller.

Both scan the analytics/tools layer and fail the moment the shape reappears.
"""

import ast
import re
from pathlib import Path

REPO_SRC = Path(__file__).parent.parent / "src" / "kroger_mcp"
SCOPED_DIRS = ["analytics", "tools"]

# Boundary resolvers: these ARE the fallback mechanism (MCP tool dispatch has
# no request/session to read a real user_id from), not a caller relying on it.
ALLOWLIST = {
    ("analytics/_user_scope.py", "resolve_user_id"),
    ("analytics/safety/_common.py", "_resolve_user_id"),
    ("tools/cart_tools.py", "_resolve_cart_user_id"),
    ("tools/shopping_list_tools.py", "_resolve_shopping_user_id"),
    ("tools/shared.py", "_resolve_pref_user_id"),
}


def _is_none_default(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _check_function(rel_path: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    if (rel_path, node.name) in ALLOWLIST:
        return []

    violations = []
    positional = node.args.posonlyargs + node.args.args
    pos_defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    for arg, default in zip(positional, pos_defaults):
        if arg.arg == "user_id" and _is_none_default(default):
            violations.append(f"{rel_path}:{node.lineno} {node.name}() -- user_id defaults to None")

    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if arg.arg == "user_id" and _is_none_default(default):
            violations.append(f"{rel_path}:{node.lineno} {node.name}() -- user_id defaults to None")

    return violations


def _find_violations() -> list[str]:
    violations: list[str] = []
    for scoped_dir in SCOPED_DIRS:
        for path in sorted((REPO_SRC / scoped_dir).rglob("*.py")):
            rel_path = str(path.relative_to(REPO_SRC))
            tree = ast.parse(path.read_text(), filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    violations.extend(_check_function(rel_path, node))
    return violations


def test_no_optional_user_id_in_scoped_modules():
    violations = _find_violations()
    assert not violations, (
        "Found user_id parameter(s) with a silent None default in "
        "analytics/ or tools/ -- this is the exact cross-tenant leak shape "
        "from the 2026-07 audit (memory note 3b195873). Require user_id "
        "explicitly and resolve it once at the MCP tool / web route boundary "
        "instead:\n  " + "\n  ".join(violations)
    )


def test_contract_detects_a_planted_violation(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("def get_thing(user_id: str | None = None):\n    pass\n")
    tree = ast.parse(planted.read_text(), filename="planted.py")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.extend(_check_function("planted.py", node))
    assert found, "contract test failed to detect a deliberately-planted violation"


# --- SHAPE 2: LEFT JOIN onto a per-user table without a user_id predicate ---

# Only LEFT JOINs are checked, and the predicate must be in the ON clause rather
# than WHERE. That is not a stylistic preference: for a LEFT JOIN, moving the
# filter to WHERE drops the unmatched-left rows and silently degrades it to an
# inner join. So "the predicate belongs in ON" is exactly right here, which keeps
# this check precise and free of false positives.
JOIN_ALLOWLIST = {
    # Rebuilds the SHARED products.category_type column from every user's stats.
    # It returns nothing to a caller, so it leaks no data across tenants; making
    # it per-user is impossible without a schema change, because category_type is
    # a single column on the shared products table, not a per-user row.
    ("analytics/categories.py", "auto_categorize_all"),
}

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\s*\)", re.S | re.I
)
_LEFT_JOIN_RE = re.compile(
    r"LEFT\s+JOIN\s+(\w+)\s+(\w+)\s+ON\b(.*?)"
    r"(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\b|\bLEFT\s+JOIN\b|\bJOIN\b|\bLIMIT\b|\"\"\"|$)",
    re.S | re.I,
)


# Prod runs Postgres, whose CREATE TABLE declares user_id explicitly (NOT NULL +
# FK to users), so pg_database.py is the authoritative answer to "is this table
# per-user?". The SQLite schema is NOT a usable second opinion: it adds user_id to
# several tables via one-time ALTER TABLE migrations rather than CREATE TABLE, so
# a static read of it under-reports.
#
# `recipes` is the one table that genuinely has user_id on Postgres and not in
# SQLite at all. Writing `AND r.user_id = ...` there raises "no such column"
# under the test backend, so it is excluded here -- closing that gap is a schema
# parity job, not a query-scoping one.
SCHEMA_DIVERGENT_TABLES = {"recipes"}


def _user_scoped_tables() -> set[str]:
    """Tables whose rows belong to one user, per the authoritative PG schema."""
    pg_schema = (REPO_SRC / "analytics" / "pg_database.py").read_text()
    return {
        m.group(1).lower()
        for m in _CREATE_TABLE_RE.finditer(pg_schema)
        if re.search(r"\buser_id\b", m.group(2))
    } - SCHEMA_DIVERGENT_TABLES


def _enclosing_function(text: str, offset: int) -> str:
    """Name of the last `def` at or before `offset`, for allowlist matching."""
    names = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", text[:offset], re.M)
    return names[-1] if names else "<module>"


def _find_unscoped_joins() -> list[str]:
    tables = _user_scoped_tables()
    assert tables, "found no user-scoped tables -- the schema scan itself is broken"

    violations = []
    for scoped_dir in SCOPED_DIRS:
        for path in sorted((REPO_SRC / scoped_dir).rglob("*.py")):
            rel_path = str(path.relative_to(REPO_SRC))
            text = path.read_text()
            for match in _LEFT_JOIN_RE.finditer(text):
                table, alias, on_clause = match.group(1).lower(), match.group(2), match.group(3)
                if table not in tables:
                    continue
                if re.search(rf"\b{re.escape(alias)}\.user_id\b", on_clause):
                    continue
                func = _enclosing_function(text, match.start())
                if (rel_path, func) in JOIN_ALLOWLIST:
                    continue
                line = text[: match.start()].count("\n") + 1
                violations.append(f"{rel_path}:{line} {func}() -- LEFT JOIN {table} {alias}")
    return violations


def test_no_unscoped_left_join_onto_a_per_user_table():
    violations = _find_unscoped_joins()
    assert not violations, (
        "LEFT JOIN onto a per-user table with no user_id predicate in the ON "
        "clause. The join key alone (product_id, recipe_id, ...) is shared "
        "across tenants, so this can pull in ANOTHER user's row. Add "
        "`AND <alias>.user_id = ?` to the ON clause and pass the owner:\n  "
        + "\n  ".join(violations)
    )


def test_join_contract_detects_a_planted_violation():
    """The guard must fail on an unscoped join and pass once it is scoped."""
    tables = _user_scoped_tables()
    assert "pantry_items" in tables, "pantry_items should be detected as per-user"

    unscoped = "LEFT JOIN pantry_items pi ON f.product_id = pi.product_id WHERE x = 1"
    scoped = (
        "LEFT JOIN pantry_items pi ON f.product_id = pi.product_id "
        "AND pi.user_id = ? WHERE x = 1"
    )

    def has_user_predicate(sql: str) -> bool:
        match = _LEFT_JOIN_RE.search(sql)
        assert match, f"regex failed to match the join in: {sql}"
        return bool(re.search(rf"\b{match.group(2)}\.user_id\b", match.group(3)))

    assert not has_user_predicate(unscoped), "guard missed a planted unscoped join"
    assert has_user_predicate(scoped), "guard false-positives on a correctly scoped join"
