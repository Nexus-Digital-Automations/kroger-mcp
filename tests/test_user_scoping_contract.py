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
# The join guard reaches further than the signature guard: web/routes also writes
# raw SQL against per-user tables, and an audit noted it was unprotected.
JOIN_SCOPED_DIRS = ["analytics", "tools", "web/routes"]

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


# --- SHAPE 2: a JOIN onto a per-user table with no user_id predicate ---

# LEFT and INNER joins are held to DIFFERENT standards, because the semantics
# differ:
#
#   LEFT JOIN -- the predicate must be in the ON clause. Not a style preference:
#     moving it to WHERE drops the unmatched-left rows and silently degrades the
#     join to an inner one.
#   INNER/plain JOIN -- the predicate may live in ON or WHERE (they are
#     equivalent here), so it only has to appear SOMEWHERE in the statement.
#
# An earlier version of this guard checked LEFT JOIN only, reasoning that inner
# joins are "usually filtered in WHERE anyway". That was wrong, and an audit
# caught it: ingredient_management_tools.py's preview_impact had a plain JOIN
# onto purchase_events with NO user filter at all, leaking any tenant's
# purchase-derived products. Absence is the bug; the ON-vs-WHERE distinction is
# only about where the fix belongs.
JOIN_ALLOWLIST = {
    # Rebuilds the SHARED products.category_type column from every user's stats.
    # It returns nothing to a caller, so it leaks no data across tenants; making
    # it per-user is impossible without a schema change, because category_type is
    # a single column on the shared products table, not a per-user row.
    ("analytics/categories.py", "auto_categorize_all"),
    # Deliberately cross-user: builds a product_id -> [owners] fan-out map so the
    # background price-drop scanner can route one price event to every user who
    # favorited that product. The fl.user_id it selects is the ROUTING key -- each
    # alert row is inserted with its own owner's user_id (notifications.py:257), so
    # no owner ever sees another's. Scoping this to one user would break the scan.
    ("analytics/notifications.py", "_favorited_products"),
}

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\s*\)", re.S | re.I
)
_CLAUSE_END = r"(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\b|\bLEFT\s+JOIN\b|\bJOIN\b|\bLIMIT\b|\"\"\"|$)"
_LEFT_JOIN_RE = re.compile(r"LEFT\s+JOIN\s+(\w+)\s+(\w+)\s+ON\b(.*?)" + _CLAUSE_END, re.S | re.I)
# Plain JOIN, excluding LEFT/RIGHT/FULL/CROSS so the two checks don't overlap.
_INNER_JOIN_RE = re.compile(
    r"(?<!LEFT )(?<!RIGHT )(?<!FULL )(?<!CROSS )(?<!OUTER )\bJOIN\s+(\w+)\s+(\w+)\s+ON\b",
    re.I,
)
# A user filter is a COMPARISON, not a mention. `GROUP BY pe.user_id` and
# `ORDER BY pe.user_id` name the column without restricting anything, so matching
# a bare `\w+\.user_id` would let a genuine leak pass by merely grouping on it.
_USER_FILTER_RE = re.compile(r"\b\w+\.user_id\s*(?:=|!=|<>|\bIN\b)", re.I)


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


def _sql_literals(source: str) -> list[tuple[str, str, int]]:
    """Every SQL-bearing string literal as (enclosing function, sql, start line).

    Bounding each scan to one literal is the point. An earlier version scanned a
    fixed character window past the JOIN keyword, which could run off the end of
    the statement and be satisfied by an unrelated `user_id` in the NEXT function
    down the file -- silently un-flagging a real leak. A literal has an end; a
    character count does not. Python's implicit concatenation ("SELECT ..." "FROM
    ...") is already joined into one node by the parser, which is the style this
    codebase writes SQL in.
    """
    literals: dict[tuple[int, int], tuple[str, str, int]] = {}
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value
            elif isinstance(node, ast.JoinedStr):
                # f-string: keep the literal parts, drop the interpolations.
                sql = " ".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            else:
                continue
            if "JOIN" in sql.upper():
                # ast.walk is breadth-first from the module, so a nested function
                # is visited after its parent and wins the attribution.
                literals[(node.lineno, node.col_offset)] = (func.name, sql, node.lineno)
    return list(literals.values())


def _find_unscoped_joins(sources: list[tuple[str, str]] | None = None) -> list[str]:
    """Join sites reading a per-user table without constraining it to one owner.

    `sources` is an optional list of (path, source-code) pairs, used by the tests
    that plant a known violation so they exercise THIS scanner rather than a
    reimplementation of its rules that could drift away from it.
    """
    tables = _user_scoped_tables()
    assert tables, "found no user-scoped tables -- the schema scan itself is broken"

    if sources is None:
        sources = [
            (str(path.relative_to(REPO_SRC)), path.read_text())
            for scoped_dir in JOIN_SCOPED_DIRS
            for path in sorted((REPO_SRC / scoped_dir).rglob("*.py"))
        ]

    violations = []
    for rel_path, source in sources:
        for func, sql, start_line in _sql_literals(source):
            if (rel_path, func) in JOIN_ALLOWLIST:
                continue

            def _report(match, table: str, alias: str, kind: str) -> None:
                line = start_line + sql[: match.start()].count("\n")
                violations.append(f"{rel_path}:{line} {func}() -- {kind} {table} {alias}")

            # LEFT JOIN: the predicate must be in the ON clause specifically.
            # Moving it to WHERE drops the unmatched-left rows, silently
            # degrading the join to an inner one.
            for match in _LEFT_JOIN_RE.finditer(sql):
                table, alias, on_clause = match.group(1).lower(), match.group(2), match.group(3)
                if table in tables and not re.search(
                    rf"\b{re.escape(alias)}\.user_id\s*=", on_clause
                ):
                    _report(match, table, alias, "LEFT JOIN")

            # Plain JOIN: ON and WHERE are equivalent, so accept a filter
            # anywhere in the statement. ANY alias counts, not just the joined
            # one, because a user-filtered driving table scopes the join
            # transitively -- `FROM meal_entries me JOIN meal_plans mp ON
            # me.plan_id = mp.id WHERE me.user_id = ?` reads only this owner's
            # entries and the join merely decorates them with the plan name they
            # already point at. Only total absence of a filter is a bug.
            for match in _INNER_JOIN_RE.finditer(sql):
                table, alias = match.group(1).lower(), match.group(2)
                if table in tables and not _USER_FILTER_RE.search(sql):
                    _report(match, table, alias, "JOIN")
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


def _scanned(sql: str) -> list[str]:
    """Run the production scanner over one planted SQL string.

    Goes through `_find_unscoped_joins` itself rather than re-deriving its rules
    inline. An earlier version of these tests reimplemented the predicate check,
    so it kept passing while the shipped rule diverged from the one under test.
    """
    module = f'def planted():\n    return conn.execute("{sql}")\n'
    return _find_unscoped_joins([("planted.py", module)])


def test_left_join_contract_detects_a_planted_violation():
    """A LEFT JOIN must carry the predicate in ON; WHERE is not good enough."""
    assert "pantry_items" in _user_scoped_tables()

    base = "LEFT JOIN pantry_items pi ON f.product_id = pi.product_id"
    assert _scanned(f"{base} WHERE x = 1"), "missed a planted unscoped join"
    assert _scanned(f"{base} WHERE pi.user_id = ?"), (
        "accepted a LEFT JOIN filtered in WHERE -- that silently degrades it to an "
        "inner join, which is exactly what this check exists to reject"
    )
    assert not _scanned(f"{base} AND pi.user_id = ? WHERE x = 1"), (
        "false-positived on a correctly scoped LEFT JOIN"
    )


def test_inner_join_contract_detects_a_planted_violation():
    """A plain JOIN may be filtered in ON or WHERE, but not nowhere."""
    assert "purchase_events" in _user_scoped_tables()

    base = "JOIN purchase_events pe ON p.product_id = pe.product_id"
    # The real shape of the leak this check was added for.
    assert _scanned(f"{base} WHERE pe.event_date >= ?"), (
        "missed a plain JOIN onto a per-user table with no user filter at all"
    )
    assert not _scanned(f"{base} WHERE pe.event_date >= ? AND pe.user_id = ?"), (
        "false-positived on a plain JOIN correctly filtered in WHERE"
    )
    assert not _scanned(f"{base} AND pe.user_id = ? WHERE x = 1"), (
        "false-positived on a plain JOIN correctly filtered in ON"
    )
    # A LEFT JOIN must not be double-reported by the inner-join check.
    assert not _INNER_JOIN_RE.search("LEFT JOIN purchase_events pe ON a.id = pe.id"), (
        "inner-join regex matched a LEFT JOIN -- the two checks would overlap"
    )


def test_naming_the_user_column_is_not_the_same_as_filtering_on_it():
    """`GROUP BY pe.user_id` restricts nothing -- it must not satisfy the check."""
    base = "JOIN purchase_events pe ON p.product_id = pe.product_id"
    for masking_clause in ("GROUP BY pe.user_id", "ORDER BY pe.user_id"):
        assert _scanned(f"{base} WHERE pe.event_date >= ? {masking_clause}"), (
            f"a bare mention in `{masking_clause}` silenced a real leak"
        )


def test_a_neighbouring_functions_filter_does_not_satisfy_the_check():
    """Scanning must stop at the end of the statement, not run into the next one."""
    module = (
        "def leaking():\n"
        '    return conn.execute("SELECT * FROM products p '
        'JOIN purchase_events pe ON p.product_id = pe.product_id")\n'
        "def unrelated():\n"
        '    return conn.execute("SELECT * FROM orders o WHERE o.user_id = ?")\n'
    )
    assert _find_unscoped_joins([("neighbours.py", module)]), (
        "an unrelated function's user filter masked the leak above it"
    )
