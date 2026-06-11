# Spec — Hybrid Per-User Kroger Credentials + Rate-Limit Resilience

Status: APPROVED (grilled 2026-06-11). Branch: `perf/phase1-event-loop-caches-per-user-token`.

## Goal

Let the Smart Shopper web app scale on **one shared Kroger app registration** for the
majority of users (one-click "Connect Kroger" OAuth — already built), while letting
**power users bring their own `client_id`/`client_secret`** for a fully isolated Kroger
rate-limit bucket. Add the rate-limit resilience (429 backoff) and response caching that
make the shared bucket stretch far enough that ordinary users never hit a limit.

## Decisions (locked via AskUserQuestion)

- **Model = Hybrid.** Shared app is the default; per-user `client_id` is optional opt-in.
  - NOTE: earlier "mandatory per-user app / force re-register / hard onboarding gate"
    answers were given under a *mandatory* model that we then rejected after establishing
    that one-click onboarding and per-user rate buckets are mutually exclusive on Kroger's
    platform (no dynamic client registration). Hybrid **supersedes** those answers:
    existing 15 users keep working on the shared app with zero disruption; new users get
    one-click; nobody is force-migrated.
- **Power-user isolation = FULL.** Their OAuth token, token **refresh**, AND product/
  location searches all run under their own `client_id`. Requires per-`client_id`
  client-credentials clients (replace the env-only module-global singleton).
- **Caching = audit + tighten.** Cache the hot public Kroger reads (product search,
  location search, chains/departments) in Redis. This is the real lever for the shared
  majority.
- **429 handling = yes**, plus 5xx, with exponential backoff + `Retry-After`.

## Background (verified in code, 2026-06-11)

- `get_kroger_credentials(user_id)` / `set_kroger_credentials(...)` already store per-user
  `client_id`/`secret`/`redirect_uri` with `KROGER_*` env fallback (`tools/shared.py:303`).
- `GET/POST /api/settings/credentials` already provide the Advanced-Settings entry UI
  (`web/routes/api/settings.py:330`), secret masked, invalidating clients on save.
- Web OAuth `connect` + `/callback` already build `KrogerAPI(client_id=creds[...])` from
  creds — BUT call `get_kroger_credentials()` **without `user_id`** → resolves the wrong
  user via `mcp_user_id()` fallback (BUG).
- `get_authenticated_client()` (`shared.py:74`) and `get_client_credentials_client()`
  (`shared.py:40`) build the client from **env only** → a power user's token, minted under
  their own `client_id`, would **refresh under the env `client_id` and fail**; their
  product searches always hit the shared bucket (no isolation).
- `KrogerClient._make_request` / `_get_token` use bare `requests.<verb>` (no `Session`),
  so the universal retry seam is a monkeypatched wrapper on those two methods.
- No Redis caching of any Kroger read today; `cache.py` `get_redis()` exists (used only by
  session validation, `sess:` keys, 300s TTL).

## Acceptance criteria (testable)

### A. Per-user credentials actually drive the clients
1. `get_authenticated_client(user_id)` builds `KrogerAPI` from `get_kroger_credentials(user_id)`
   (client_id/secret/redirect_uri), not env — so refresh uses the minting `client_id`.
2. `get_client_credentials_client(user_id=None)` resolves creds per-user and **caches one
   client per distinct `client_id`** (dict, not a single global). Two users with different
   `client_id`s get different cached clients; same `client_id` reuses one.
3. Client-credentials token cache file is keyed per `client_id`
   (`.kroger_token_cc_<sha256(client_id)[:12]>.json`); the existing env-app file keeps
   working (back-compat).
4. Unit test: a user with custom creds → builder receives that `client_id`; a user without
   → env `client_id`. (monkeypatch `KrogerAPI`/`get_kroger_credentials`.)

### B. Multi-user `user_id` plumbing bugs fixed
5. `start_oauth()` and `/callback` resolve `get_kroger_credentials(user_id=current_user_id(request))`.
6. `save_credentials()` invalidates the **caller's** clients (`invalidate_*client(user_id)`),
   and changing creds clears that user's stored Kroger token (token minted under the old
   `client_id` is invalid under the new one) so they must re-link.
7. The 5 web client-credentials call sites that have `request`
   (`api/products.py:232,362`, `api/deals.py:95,183`, `routes/settings.py:173`) pass
   `current_user_id(request)`.

### C. 429 / 5xx backoff
8. New `tools/_kroger_retry.py` `install_kroger_retry()` idempotently wraps
   `KrogerClient._make_request` and `_get_token`: on HTTP 429/500/502/503/504, retry with
   exponential backoff (base 0.5s, factor 2, jitter) up to 4 attempts, honoring a
   `Retry-After` header when present; re-raise after the last attempt. Installed at
   `tools/shared.py` import (idempotent guard) so every client path is covered.
9. Structured-logger WARN on each retry (`event=kroger_rate_limited`, attempt, status,
   sleep) and ERROR on final give-up. Never logs secrets.
10. Unit test: a fake response sequence (429, 429, 200) returns the 200 after 2 sleeps
    (sleep patched); a persistent 429 raises after 4 attempts.

### D. Caching the hot public reads
11. Product search, location search, and chains/departments results are cached in Redis,
    keyed by **`client_id` + normalized params** (so power users' caches are isolated and a
    shared-bucket user benefits from warm entries). TTLs: products 1h, locations 6h,
    chains/departments 24h. Cache-miss path unchanged; Redis-down degrades to direct call.
12. Unit test: second identical search within TTL hits Redis (underlying Kroger call
    invoked once); different `client_id` → separate cache entry (no cross-tenant leak).

### E. Non-regression / safety
13. Existing MCP callers (no `user_id`) behave exactly as before (env app creds, shared
    client) — `mcp_user_id()` fallback preserved.
14. No secret ever logged or returned unmasked. `git check-ignore` still covers `.env`.
15. `ruff` + `mypy --strict` clean; full test suite green; build succeeds.

## Out of scope (explicitly)
- Guided onboarding wizard (existing Advanced-Settings form is sufficient for the power-user
  minority).
- Requesting a higher rate limit from Kroger for the shared app — an ops/email action, noted
  in the runbook, not code.
- MCP-tool-layer user_id threading / contextvars — web routes build clients locally; the MCP
  path stays on `mcp_user_id()`.

## Tasks
- [ ] A: per-user creds drive `get_authenticated_client` + per-client_id `get_client_credentials_client` cache
- [ ] C: `_kroger_retry.py` 429/5xx backoff chokepoint + install on import
- [ ] D: Redis caching for product/location/chains/departments keyed by client_id+params
- [ ] B: fix start_oauth/callback/save_credentials user_id plumbing + clear token on cred change + web cc call sites
- [ ] Tests: credential resolution, retry sequence, cache hit/isolation, regression
- [ ] ruff + mypy + full test suite + build green; commit + push
