# Phase 1 Spec — Event-loop unblock + caches + per-user Kroger token

Branch: `perf/phase1-event-loop-caches-per-user-token`. Full design: `~/.claude/plans/okay-please-plan-out-imperative-feather.md`.

## Acceptance criteria (testable)

### AC-1 Non-blocking chat (the core ceiling)
- A1.1 A chat request no longer runs `requests.post` on the event loop; LLM calls use a shared per-worker `httpx.AsyncClient`.
- A1.2 With one in-flight slow chat, a concurrent `GET /api/products/search` returns without waiting for the chat (no head-of-line block). Provable via the load test.
- A1.3 Chat responds as `text/event-stream`, emitting `event: token` incrementally, then `event: done` carrying the updated `messages` array. Mutating actions emit exactly one `event: pending_action`; `/api/chat/approve` still executes it synchronously.
- A1.4 Read-only tool handlers invoked during a chat turn run via `asyncio.to_thread` (do not block the loop).
- A1.5 Streamed `done.messages` for a read-only query equals the legacy `process_message` output for the same input (golden test, mocked provider).

### AC-2 Multi-worker safety
- A2.1 App boots with ≥2 uvicorn workers; no module-level mutable cache is created pre-fork.
- A2.2 PG pool sized so workers × max_size ≤ Postgres max_connections; no "connection in use"/cross-fork socket errors under load.
- A2.3 `_authenticated_client` global is removed.

### AC-3 Session cache
- A3.1 `validate_session` is read-through Redis (`sess:{sha256(token)}`, TTL 300s); a second validation within TTL does not hit the DB.
- A3.2 Logout / expiry / deactivate delete the cache key.
- A3.3 Redis unavailable → auth still works via DB fallthrough (no exception surfaced to the request).

### AC-4 Ingredient pattern cache
- A4.1 Patterns compiled once at startup; not recompiled per request.
- A4.2 A custom-ingredient write bumps a Redis `ingredients:version`; the next scan rebuilds. Redis down → TTL fallback, no crash.

### AC-5 Per-user encrypted Kroger token (fixes multi-user correctness bug)
- A5.1 `get_authenticated_client(user_id)` returns a client built from that user's DB token; `load_kroger_token(A) != load_kroger_token(B)` for distinct connected users.
- A5.2 `access_token`/`refresh_token` are ciphertext at rest (DB value ≠ plaintext); `decrypt(encrypt(x)) == x`; tampered ciphertext raises; missing master key raises (never silent plaintext).
- A5.3 Token refresh persists per-user to the DB (no shared file write); no shared `_authenticated_client`.
- A5.4 `kroger_tokens` table exists in both PG and SQLite schemas.
- A5.5 `scripts/migrate_kroger_token.py` moves the existing `.kroger_token_user.json` to its owner idempotently and renames the file `.migrated`.

### AC-6 Quality gates
- A6.1 Existing 27 test suites green.
- A6.2 mypy (build-digester) + ruff (lint-digester) clean on every touched file.
- A6.3 New critical-path tests for AC-1.5, AC-3, AC-5 pass.
