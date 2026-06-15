# Gunicorn 2 workers + kroger-mcp leak fix + MCP hub consolidation

## Context
Capture the measured ~2.5× heavy-path throughput SAFELY (gunicorn fork-workers,
not bare `uvicorn --workers` which dies under launchd), fix the per-session
kroger-mcp process leak in code, and consolidate the stateless MCP hub
(deepwiki+github) onto the dedicated `macmini2` (`mcp`) box to reclaim ~250 MB on
prod (`macmini1`). Full design: `~/.claude/plans/okay-please-plan-out-imperative-feather.md`.

## Acceptance criteria

### A — Gunicorn
- [ ] `gunicorn` is a declared dependency; `uv.lock` updated.
- [ ] `kroger-web` (`app.run()`) launches gunicorn + `UvicornWorker`,
      `preload_app=False`, `workers=$WEB_WORKERS`, bind `0.0.0.0:$WEB_PORT`,
      `timeout=120`. `stop()` still runs first.
- [ ] Prod runs 2 `UvicornWorker` procs under a gunicorn master on :8000;
      `/login` 200; 5 probes 200; `vm.swapusage` stays flat (no thrash).
- [ ] `scripts/loadtest.py` re-run shows ~2× heavy-path vs the 1-worker baseline.

### B — kroger-mcp leak fix
- [ ] `server.py main()` installs an idle watchdog that exits after
      `KROGER_MCP_IDLE_TIMEOUT` (default 1800 s) of no MCP activity, plus clean
      `SIGTERM`/`SIGHUP` and orphan (`getppid()==1`) exit.
- [ ] Activity resets the idle timer (verified via FastMCP 3.3.1 middleware/hook).
- [ ] Unit test: watchdog fires after a short test timeout; activity resets it.
- [ ] After a Claude session ends, no kroger-mcp procs accumulate on prod.
- [ ] ruff + mypy clean; pytest green.

### C — MCP hub consolidation
- [ ] `macmini2` runs `com.local.mcp-hub` (mcp-proxy: deepwiki+github) bound to
      100.105.113.44:9090, RunAtLoad + KeepAlive; serves over Tailscale.
- [ ] `github.cfg` present on macmini2 (mode 600); no token echoed to logs/context.
- [ ] This laptop's `~/.claude.json` deepwiki/github entries point at macmini2;
      kroger→macmini1, ldr/playwright→macmini2 unchanged. (Other clients flagged.)
- [ ] Prod `com.local.mcp-hub` + `hub-run.sh` removed; node procs gone; ~250 MB
      reclaimed (prod free RAM comfortably above the prior ~66 MB with 2 workers).
- [ ] Stale `mempalace-run.sh` removed on both minis.
- [ ] Fresh Claude session: deepwiki + github tools resolve via macmini2; kroger
      + ldr still work per-session.

## Constraints
- Graceful restarts only (`bootout`→`bootstrap`, NEVER `kickstart -k`).
- Bring up macmini2 hub BEFORE stripping prod's (no MCP downtime).
- Secrets: back up before editing; mode 600; never echo tokens.
- Roll back to `WEB_WORKERS=1` if prod swap climbs; keep plist `.bak` + prod hub
  scripts until macmini2 hub is verified.
