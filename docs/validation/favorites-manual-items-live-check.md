# Live verification — favorites manual items

**Date:** 2026-08-29
**Target:** the live `kroger` MCP server (`.mcp.json` → ssh `macmini1` →
`~/.config/mcp/kroger-run.sh` → `uv run --frozen kroger-mcp`), handshake
`Kroger API Server 3.3.1`, 18 tools.

Everything below was driven over the server's stdio JSON-RPC transport
directly, because a session's MCP tool list is fixed at startup and this
session began before the server was attached.

`scripts/smoke_mcp.py` remains the repeatable full-surface sweep; it invokes
every action with fixed safe arguments and cannot carry state between calls,
so this check — which threads a generated `manual:` id from one call into the
next — was run as a one-off client instead.

## Is the live server actually running this code?

The mini's git checkout reports HEAD `a533e6f` (2026-06-22), which is *not*
this work — but that is misleading. Deployment there is a file sync onto a
stale checkout, and the venv is an editable install (`_editable_impl_kroger_mcp.pth`
→ `/Users/macmini1/kroger-mcp/src`), so the server imports the working-tree
files, not HEAD. Comparing the files that matter, local vs live:

| file | sha256 (first 12) |
| --- | --- |
| `tools/_cart_safety.py` | `50d2c3352b56` |
| `tools/meal_planner_tools.py` | `709620dcab4d` |
| `tools/cart_tools.py` | `6d3dbad155b7` |
| `analytics/favorites.py` | `d997c0e92313` |
| `tools/favorites_tools.py` | `a898910b9b37` |
| `web/routes/api/cart.py` | `b1385428de12` |

All six identical on both sides — the live process runs exactly the guarded
code. (Check the hashes, not the mini's `git log`.)

## What was exercised

Read-only, favorites writes, and previews only. The `order` call used
`confirm=False` and the `cart` calls used `preview_only=True`; nothing was
sent to the real Kroger cart.

### 1. Create a manual favorite, with and without a reason

`favorites(action='add_item', list_id=…, description='sourdough starter',
manual=True, override_reason='farmers market only, not sold at Kroger')`

```json
{"success": true, "product_id": "manual:2fbd2097346447dfb8308cde95f71b4a",
 "description": "sourdough starter", "is_manual": true,
 "override_reason": "farmers market only, not sold at Kroger"}
```

Same call with `manual=True` and no reason ("backyard rosemary") also
succeeded, returning `"override_reason": null` — the optional-reason decision
holds on the live schema, so the new columns exist in the production database.

### 2. `get_items` exposes the manual fields

The list read back three items — two manual, one product-linked
(`0081829001699`, Chobani Dairy Free Vanilla Oat Milk) — each carrying
`is_manual` and `override_reason`. Pre-existing rows in the user's real
`weekly-essentials` list read back `is_manual: false` / `override_reason: null`,
so the migration did not disturb them.

### 3. `order` preview surfaces MANUAL PURCHASE and still orders the rest

`favorites(action='order', confirm=False, skip_if_stocked=False)`

```json
{"preview": {
   "items_to_order": [{"product_id": "0081829001699", …}],
   "manual_purchase": [
     {"product_id": "manual:609be3…", "description": "backyard rosemary",
      "override_reason": null, "action": "MANUAL"},
     {"product_id": "manual:2fbd20…", "description": "sourdough starter",
      "override_reason": "farmers market only, not sold at Kroger",
      "action": "MANUAL"}],
   "order_count": 1, "manual_count": 2},
 "next_step": "… Items under MANUAL PURCHASE are not sold at Kroger — you'll
               need to source those yourself."}
```

Split, not dropped: the linked item still orders, the manual ones are named
in the response with their reasons.

### 4. The cart path refuses the sentinel id

Single: `cart(action='add', product_id='manual:2fbd20…')` →

```json
{"success": false, "error": "These are manual items not sold at Kroger and
 cannot be added to the cart: manual:2fbd2097346447dfb8308cde95f71b4a.
 You'll need to source them yourself."}
```

Batch: `cart(action='add', items=[{linked}, {manual}])` → the **same** refusal.
The whole batch fails; the linked item is not partially ordered.

### Cleanup

The temporary list was deleted (`items_deleted: 3`); the user's five original
lists are untouched.

## What this did NOT cover

The *laundering* path — a manual id attached to a recipe ingredient via
`recipes(action='link_ingredient')`, which forces `override=False` and so
slips past every boolean-based filter — is the deepest part of the fix and is
covered by `test_shared_cart_gate_rejects_a_manual_id_unconditionally`. It was
not exercised live because reaching `check_cart_items_safety` on that path
requires `confirm=True`, i.e. a genuine attempt to write to the real Kroger
cart. The session's standing write scope requires asking before any real cart
write, so it was left for the user to authorize.
