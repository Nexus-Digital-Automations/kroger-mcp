# Worker scaling — measure + tune for the hardware

## Context
User asked how high concurrency could go if maximized for the 8-core / 8 GB
shared mini, then approved the high-value moves. mempalace removal freed ~1.3 GB
(the original reason WEB_WORKERS was pinned at 1). Goal: replace estimates with
measured numbers, then raise prod worker count to the data-supported value
within the shared-box memory budget — without regressing stability or being a
bad neighbor.

## Approach
1. Local httpx load harness (`scripts/loadtest.py`) — register+login a throwaway
   user, sweep concurrency [1,4,8,16,32,64] against `/login` (event-loop/template
   path) and `/dashboard` (the `run_in_thread` DB path). $0; no Kroger endpoints.
2. Run the sweep at WEB_WORKERS = 1, 2, 4 locally; record req/s + p50/p95/p99.
3. Apply the data-supported worker count to prod via the launchd plist
   `EnvironmentVariables` (graceful bootout→bootstrap, never `kickstart -k`).
4. Update the runbook's WEB_WORKERS guidance.

## Acceptance criteria
- [ ] Load harness runs locally, gets a 200 on `/dashboard`, prints per-level
      throughput + percentiles for both paths.
- [ ] Measured comparison across WEB_WORKERS 1/2/4 captured (where throughput
      plateaus and p95 starts degrading).
- [ ] Prod worker count raised to the chosen value (memory-bounded; default
      target 2) and the plist reflects it.
- [ ] Post-change prod validation: /login 200, 5 stability probes 200, and
      **swap/used memory stays healthy** (no thrash). Roll back to 1 if swap climbs.
- [ ] PG: workers × PG_POOL_MAX stays well under PG max_connections.
- [ ] Runbook updated: WEB_WORKERS guidance reflects the new value + rationale;
      graceful-restart requirement retained. Committed + deployed.

## Caveats (record honestly)
- Local DB is SQLite; prod is Postgres (the PG pool ceiling of 8/worker only
  binds on prod). Local numbers measure event-loop + thread-pool + worker scaling,
  not the PG pool wall.
- Shared box: the practical max is a *fair-share* max; biggest RAM win is
  relocating other MCP servers, not tuning this app.
