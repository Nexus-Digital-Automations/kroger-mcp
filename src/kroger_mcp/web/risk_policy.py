"""Risk classification for chatbot-triggered MCP tool calls.

Decides whether a (tool, action) call is safe to auto-execute or must stop
for user approval, based on the selected chat mode:
  - read_only:    always auto-executes, in both Ask and Auto mode.
  - write:        confirmed in Ask mode, auto-executed in Auto mode.
  - hard_blocked: always confirmed, regardless of mode — reserved for real
                  Kroger cart writes/checkout and irreversible account
                  actions (data deletion).

Default classification is a verb heuristic; the override table below covers
every exception the heuristic gets wrong, plus every hard-blocked action.
"""

from __future__ import annotations

from typing import Any, Literal

Tier = Literal["read_only", "write", "hard_blocked"]

_READ_VERB_PREFIXES = (
    "get",
    "list",
    "view",
    "check",
    "search",
    "find",
    "preview",
    "suggest",
    "analyze",
    "export",
    "score",
)

# Cart-equivalent actions default to a harmless preview and only actually
# write when the caller sets the execute flag — so their hard_blocked tier
# only applies when that flag is set to the "unsafe" (writing) value.
_ArgGatedOverride = tuple[Tier, tuple[str, Any]]

_OVERRIDES: dict[tuple[str, str], Tier | _ArgGatedOverride] = {
    # --- hard-blocked: real Kroger cart / checkout mutations ---
    ("cart", "add"): ("hard_blocked", ("preview_only", False)),
    ("cart", "remove"): "hard_blocked",
    ("cart", "clear"): "hard_blocked",
    ("cart", "mark_placed"): "hard_blocked",
    ("favorites", "order"): ("hard_blocked", ("confirm", True)),
    ("meal_plan", "add_to_cart"): ("hard_blocked", ("confirm", True)),
    ("shopping_list", "add_to_cart"): ("hard_blocked", ("confirm", True)),
    ("recipes", "add_to_cart"): ("hard_blocked", ("confirm", True)),
    ("privacy", "delete_my_data"): "hard_blocked",
    # --- explicit normal-write judgment calls (not hard-blocked) ---
    ("privacy", "set_consent"): "write",
    ("privacy", "withdraw"): "write",
    # --- heuristic misses: reads whose verb isn't in _READ_VERB_PREFIXES ---
    ("favorites", "suggest"): "read_only",
    ("favorites", "check_snacks"): "read_only",
    ("favorites", "get_low_stock"): "read_only",
    ("pantry", "get_attention"): "read_only",
    ("pantry", "list_gaps"): "read_only",
    ("reports", "*"): "read_only",
    ("notion", "get_status"): "read_only",
    ("notion", "view_recipe"): "read_only",
    ("info", "list_chains"): "read_only",
    ("info", "get_chain"): "read_only",
    ("info", "check_chain"): "read_only",
    ("info", "list_departments"): "read_only",
    ("info", "get_department"): "read_only",
    ("info", "check_department"): "read_only",
    ("info", "get_datetime"): "read_only",
    ("info", "get_servings"): "read_only",
    ("info", "get_preferences"): "read_only",
}


def classify(tool: str, action: str | None, args: dict[str, Any]) -> Tier:
    """Classify a tool call's risk tier for approval-gating purposes."""
    override = _OVERRIDES.get((tool, action or "")) or _OVERRIDES.get((tool, "*"))
    if override is not None:
        if isinstance(override, tuple):
            tier, (flag, unsafe_value) = override
            actual = args.get(flag, not unsafe_value if isinstance(unsafe_value, bool) else None)
            return tier if actual == unsafe_value else "read_only"
        return override
    if action and action.split("_")[0] in _READ_VERB_PREFIXES:
        return "read_only"
    return "write"  # unknown/ambiguous actions default to confirm, never silent auto-run
