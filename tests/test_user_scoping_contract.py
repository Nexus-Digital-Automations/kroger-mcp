"""Regression guard for the 2026-07 multi-tenant user_id scoping audit.

The leak that audit found wasn't one bug -- it was one recurring shape: a
function takes `user_id` but treats a missing value as fine, silently
resolving it to a shared default owner instead of making the caller supply
the real one. This scans the analytics/tools layer and fails the moment
that shape reappears, so the class of bug can't quietly come back.
"""

import ast
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
