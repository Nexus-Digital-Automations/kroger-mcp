# Frontend Audit — Bugs Found
**Date:** 2026-05-03
**Spec:** [`specs/frontend-audit-pass.md`](../specs/frontend-audit-pass.md)
**Method:** Chrome DevTools MCP walk of every in-scope page,
exercising every interactive element and comparing observed behavior
to the visible promise of the UI.

| # | Where | Severity | Description | Status |
|---|-------|----------|-------------|--------|
| 1 | `src/kroger_mcp/web/routes/cart.py` + `templates/cart.html` | Low (dead code) | Orphan page route — `app.include_router(cart.router)` was never called; only `api_cart` (the API namespace) and `shopping_list.py`'s `/cart → /shopping-list` redirect are wired. The page module + template were leftover from before the cart-view removal spec. Reachable only by running the file directly. | **Fixed** — deleted both files. |
