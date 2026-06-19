"""Closes two verification gaps for the cost-card feature:

1. MCP tool JSON via a real in-process FastMCP client round-trip — exercises the
   session's `recipes` tool (analyze + preview_order) with include_spices on/off.
   The registered kroger MCP runs remotely on prod (old code), so this drives the
   local session code instead.
2. The /recipes LIST card cost — confirms the card's per-serving value reflects
   spice exclusion by default (served HTML's embedded recipe JSON).

Run with PYTHONPATH=src and the web server up on BASE.
"""

import asyncio
import html
import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:8099"
RECIPE_ID = "af6523bb"  # Coconut Curry Chicken
with open("/tmp/ss_token.txt") as f:
    TOKEN = f.read().strip()

failures: list[str] = []


def _tool_payload(result):
    """FastMCP call_tool result -> dict, across client/version shapes.

    Handles: CallToolResult(.data/.structured_content/.content), a bare list of
    content blocks, or a (content, structured) tuple.
    """
    data = getattr(result, "data", None)
    if data is not None:
        return data
    sc = getattr(result, "structured_content", None)
    if sc:
        return sc
    content = getattr(result, "content", None)
    if content is None:
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            return result[1]
        content = result  # bare list of content blocks
    block = content[0]
    text = getattr(block, "text", None)
    if text is None and isinstance(block, dict):
        text = block.get("text")
    return json.loads(text)


async def check_mcp():
    from fastmcp import Client

    from kroger_mcp.server import create_server

    server = create_server()
    async with Client(server) as client:
        tools = {t.name for t in await client.list_tools()}
        assert "recipes" in tools, f"recipes tool missing; have {sorted(tools)}"

        # --- analyze: default (spices excluded) vs include_spices ---
        a_def = _tool_payload(
            await client.call_tool("recipes", {"action": "analyze", "recipe_id": RECIPE_ID})
        )
        a_inc = _tool_payload(
            await client.call_tool(
                "recipes",
                {"action": "analyze", "recipe_id": RECIPE_ID, "include_spices": True},
            )
        )
        if "cost_estimate" not in a_def:
            print("DEBUG analyze default keys:", list(a_def.keys()))
            print("DEBUG analyze default payload:", json.dumps(a_def)[:600])
        ce_def = a_def["cost_estimate"]
        ce_inc = a_inc["cost_estimate"]
        bd = ce_def["breakdown"]
        print(
            f"analyze default: total={ce_def['total_cost']} cps={ce_def['cost_per_serving']} "
            f"include_spices={ce_def.get('include_spices')}"
        )
        print(
            f"analyze include: total={ce_inc['total_cost']} cps={ce_inc['cost_per_serving']} "
            f"include_spices={ce_inc.get('include_spices')}"
        )

        # every entry exposes the new fields
        for e in bd:
            for key in ("cost_per_serving", "is_spice", "excluded_from_total"):
                if key not in e:
                    failures.append(f"analyze breakdown entry missing '{key}': {e.get('ingredient')}")
                    break
        spices = [e for e in bd if e["is_spice"]]
        if not spices:
            failures.append("analyze: expected some is_spice ingredients, found none")
        if not all(e["excluded_from_total"] for e in spices):
            failures.append("analyze default: some spices not marked excluded_from_total")
        if ce_def.get("include_spices") is not False:
            failures.append("analyze default: include_spices flag not False on result")
        if ce_inc.get("include_spices") is not True:
            failures.append("analyze include: include_spices flag not True on result")
        inc_spices = [e for e in ce_inc["breakdown"] if e["is_spice"]]
        if any(e["excluded_from_total"] for e in inc_spices):
            failures.append("analyze include: a spice still marked excluded_from_total")
        if (
            ce_def["total_cost"] is not None
            and ce_inc["total_cost"] is not None
            and not ce_inc["total_cost"] > ce_def["total_cost"]
        ):
            failures.append(
                f"analyze: include_spices should raise total "
                f"({ce_def['total_cost']} -> {ce_inc['total_cost']})"
            )

        # --- preview_order: per-ingredient is_spice + cost_estimate block ---
        p = _tool_payload(
            await client.call_tool("recipes", {"action": "preview_order", "recipe_id": RECIPE_ID})
        )
        ings = p.get("ingredients", [])
        if not any("is_spice" in i for i in ings):
            failures.append("preview_order: ingredients missing is_spice flag")
        if "cost_estimate" not in p or p["cost_estimate"] is None:
            failures.append("preview_order: missing cost_estimate block")
        else:
            n_spice = sum(1 for i in ings if i.get("is_spice"))
            print(
                f"preview_order: {len(ings)} ingredients, "
                f"cost_estimate.cps={p['cost_estimate'].get('cost_per_serving')}, "
                f"spices flagged={n_spice}"
            )


def _embedded_recipes(page: str):
    """Pull the recipe list array that carries cost/health (not the id+name
    autocomplete index). Scans every embedded JSON array and picks the one whose
    items expose a 'cost' key."""
    text = html.unescape(page)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[\{", text):
        try:
            arr, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) and "cost" in arr[0]:
            return arr
    return None


def check_list_card():
    req = urllib.request.Request(f"{BASE}/recipes", headers={"Cookie": f"kroger_session={TOKEN}"})
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (localhost only)
        page = resp.read().decode()
    data = _embedded_recipes(page)
    if not data:
        failures.append("list page: could not locate embedded recipes JSON with cost")
        return
    row = next((r for r in data if r.get("id") == RECIPE_ID), None)
    if not row:
        failures.append("list page: target recipe not in embedded JSON")
        return
    print(f"list card cost for {RECIPE_ID} (spice-excluded per-serving) = {row.get('cost')}")
    if row.get("cost") is None:
        failures.append("list card cost is null for a recipe that prices fine")
    elif abs(row["cost"] - 8.82) < 0.01:
        failures.append("list card shows spices-INCLUDED cost; expected spice-excluded 7.08")
    elif abs(row["cost"] - 7.08) >= 0.01:
        failures.append(f"list card cost {row['cost']} != expected spice-excluded 7.08")


async def main():
    await check_mcp()
    check_list_card()
    print("\n=== RESULT ===")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: MCP analyze+preview_order JSON carry new spice/per-serving fields;")
    print("      list card cost reflects spice exclusion by default.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
