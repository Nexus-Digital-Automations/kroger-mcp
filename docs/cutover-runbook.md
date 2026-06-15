# `prod` Cutover Runbook — SQLite → Postgres on the production mini

> ## ✅ EXECUTED & COMPLETE — 2026-06-11
> The production app on `prod` is live on **PostgreSQL 16** (`:5433`, localhost) + **Redis**
> (`:6379`, localhost). ETL parity: **34/34 tables**, 7 orphan rows dropped (deleted-user
> detritus), 1 kroger token migrated. Verified: `get_backend()==postgresql`, login/auth works,
> redis cache live, `:8000` serving. Real data intact (users=15, orders=17, price_history=5805…).
>
> **Gotchas hit & fixed during execution (for next time / the dev box):**
> - PG runs on **:5433** (not 5432). `pg_hba`: socket=`peer` (passwordless admin bootstrap),
>   loopback TCP=`scram-sha-256` (app role) — committed in `provision_prod.sh` (`5b8ca2f`).
> - **Boolean idiom bug**: SQLite `col = 1/0` broke on PG (`boolean = integer`) — broke auth.
>   Fixed centrally in `_translate_sql` (`511738f`), normalizing the 12 BOOLEAN columns.
> - **Redis 7**: `CONFIG REWRITE` stored the password as an ACL `user default … #<hash>` line,
>   not `requirepass`; reset to `nopass` (localhost-bound + protected-mode is the boundary).
> - Secrets live in prod `~/kroger-mcp/.env` (gitignored, 600): `DATABASE_URL`, `REDIS_URL`,
>   `APP_ENV=prod`, `WEB_WORKERS=2`, `KROGER_TOKEN_MASTER_KEY`. Keychain was unreliable over SSH.
>
> **🔁 ROLLBACK (tested-ready, not executed):** the original SQLite at
> `~/kroger-mcp/data/kroger_analytics.db` is **untouched** (the ETL only read it; integrity `ok`).
> To revert: remove the `DATABASE_URL` line from `~/kroger-mcp/.env`, then
> `launchctl kickstart -k gui/$(id -u)/com.smartshopper.web`. `get_backend()` falls back to
> `sqlite` and the app serves the pre-migration data. The off-box backups under
> `data/backups/prod/` (pre-cutover SQLite + post-cutover PG dump) are the disaster copies.
>
> **Open (need your input / sudo):** enable the macOS firewall on prod (sudo; PG/Redis are
> already localhost-bound so this is defense-in-depth); decide whether to move the lightweight
> `github`/`mempalace` MCP servers off prod (they're your Claude tooling, not the app).

> ## 🧠 MEMORY-PRESSURE FAILURE MODE — 2026-06-12 (read before diagnosing "wedges")
> The mini has **8 GB RAM**; when the mempalace miner runs hot (1.2–2.5 GB RSS,
> ~400% CPU) the box goes 30+ GB into swap and Smart Shopper gets **paged out**.
> Symptoms that look like an app hang but are NOT: `/login` curls return `000`
> while the process is alive and `:8000` is listening; first request after idle
> takes 6–8 s then everything is fast; **boot takes 60–90 s** (vs ~5 s healthy) so
> a 45 s deploy health-wait reports a false failure; `launchctl bootstrap` can
> flake with `5: Input/output error` (re-run it). Diagnose with
> `sysctl vm.swapusage` + `ps -amcwwwxo "rss pid etime command" | head` BEFORE
> assuming an event-loop wedge. Use a ≥120 s health-wait in deploy scripts.
> App-side hardening already in place (2026-06-12): every Kroger HTTP call has a
> default timeout (`_kroger_retry._TimeoutEnforcingRequests`, the lib itself sets
> none) and every Kroger-calling web route runs off the event loop.
> Real fixes are box-level: tame/schedule the miner, add RAM, or move Smart
> Shopper to the .108 box (this runbook's original purpose).
>
> **UPDATE 2026-06-13 — root cause removed.** The mempalace miner (a launchd
> agent `com.user.mempalace-remote-mine` running CPU embeddings over all
> `~/.claude/projects` transcripts) was the swap driver. It has been fully
> removed from the mini (launchd agent, store, run script, binary) and the
> laptop. Mini swap dropped 33 GB → ~1.7 GB; prod serves sub-10 ms. If
> swap-thrash recurs, look for a NEW heavy tenant — mempalace is no longer it.

> ## ⚙️ EFFICIENCY DEPLOY — 2026-06-11 (good-neighbor batch)
> Deployed gzip, shared Jinja2 templates, static cache headers, 5 perf indexes
> (`add_perf_indexes.py`, CONCURRENTLY), an order-history N+1 fix, and Redis caching
> (recommendations + favourites). **Two hard-won prod gotchas — MUST preserve:**
> - **`WEB_WORKERS=1` is REQUIRED on prod** (set via the launchd plist
>   `EnvironmentVariables`, `.bak` kept alongside it). uvicorn 0.47 `workers=2` uses
>   macOS multiprocessing **`spawn`**, whose workers **die silently under launchd**
>   (service restart-loops, `:8000` never binds). `workers=1` runs in-process (no spawn)
>   and is stable — and is the right good-neighbor choice on this SHARED box (mempalace
>   ~1.3 GB + remote tooling also run here). The async event loop still handles many
>   concurrent connections in one worker. To get true multi-worker later, front it with
>   gunicorn's uvicorn worker (fork-based), not bare `uvicorn --workers`.
> - **uvloop/httptools were reverted** — uvloop's event-loop re-init across the same
>   `spawn` workers also fails under launchd. Default asyncio loop is the stable path here.
> - `run()`'s `stop()` now **waits for the port to release** before binding (was a
>   SIGTERM-and-return race that, under launchd KeepAlive, spiralled into orphaned
>   non-serving workers — exactly what a hard `kickstart -k` can trigger). **Use graceful
>   `launchctl kickstart` (no `-k`) or bootout→bootstrap for restarts.**
> - The prod `.env` line should read **`WEB_WORKERS=1`** (the plist env also enforces it).

> ## 📈 WORKER-SCALING TEST — 2026-06-15 (stays at 1; measured the headroom)
> Question: how high can concurrency go on this 8-core/8 GB SHARED box? Measured
> with `scripts/loadtest.py` (httpx sweep over `/login` + `/dashboard`, local,
> $0, no Kroger endpoints). prod backend is **SQLite (WAL, busy_timeout 5 s)**,
> so the local SQLite run is representative.
> - **1 worker:** `/login` peaks ~260 req/s; `/dashboard` (the `run_in_thread`
>   DB path) plateaus **~33 req/s**.
> - **2 workers:** `/dashboard` ~**2.5×** (~83 req/s peak); cheap-path tail ~2× better.
> - **4 workers:** cheap path scales (login ~660 req/s) but `/dashboard` regressed
>   — SQLite write-lock contention across processes (real on prod too, same backend).
> A 2-worker bump was applied + validated (2 workers served `:8000`, swap stayed
> **flat at 256 M**, probes 200) **then ROLLED BACK to 1**. Why: prod is still
> **uvicorn 0.47.0** — the version the spawn-under-launchd hazard above was written
> against — and the box was left at **~66 M unused** (the warm `kroger-mcp` MCP
> servers + system apps are the real RAM tenants, not the web app). The 2× isn't
> needed for a household workload, and bare `uvicorn --workers` remains the
> documented-unsafe path under launchd KeepAlive crash-recovery (untested on live
> prod). **To actually capture multi-worker safely: gunicorn + `UvicornWorker`
> (fork-based), not bare `uvicorn --workers`** — and/or free RAM by relocating the
> MCP-server neighbors off the mini.

> ## ✅ MULTI-WORKER SHIPPED + MCP HUB MOVED — 2026-06-15 (follow-up)
> Did both of the above. **Prod now runs 2 workers via gunicorn + UvicornWorker**
> (`app.run()` launches `gunicorn.app.base.BaseApplication`, `preload_app=False`,
> `worker_class=uvicorn.workers.UvicornWorker`, `timeout=120`; plist
> `WEB_WORKERS=2`). Fork-based = no spawn-under-launchd death; validated: gunicorn
> master + 2 workers on :8000, probes 200, **swap flat at 256 M**. Bare
> `uvicorn --workers` is gone — do NOT reintroduce it.
> - **kroger-mcp leak fixed in code** (`server.py main()`): an idle watchdog
>   (daemon thread) exits the process after `KROGER_MCP_IDLE_TIMEOUT` (default
>   1800 s) of no MCP activity, or when orphaned (`getppid()==1`), plus
>   SIGTERM/SIGHUP exit. Activity tracked via a FastMCP middleware heartbeat
>   (needs fastmcp ≥3; floor bumped, import degrades gracefully on older). Stops
>   the per-session ssh-stdio process accumulation.
> - **Shared MCP hub relocated prod → `macmini2` (`mcp`, 100.105.113.44).** The
>   stateless deepwiki+github mcp-proxy hub now runs there
>   (`com.local.mcp-hub`, `~/.config/mcp/hub-run.sh`, bound 100.105.113.44:9090).
>   Prod's `com.local.mcp-hub` + `hub-run.sh` retired to `.bak`; node procs gone;
>   **prod freed to ~674 M unused with the 2 workers running**. Client
>   `~/.claude.json` deepwiki/github point at macmini2. kroger stays on prod
>   (stateful, coupled to prod's SQLite); ldr/playwright stay per-session
>   ssh-stdio (per-user/stateful) — NOT hub-appropriate.
> - **Roll back**: prod plist `.bak` (WEB_WORKERS) + retired `com.local.mcp-hub.plist.bak`
>   / `hub-run.sh.bak` on macmini1 restore the old single-worker + local-hub setup.

---

Ordered, destructive-step-gated runbook for moving Smart Shopper's real data onto
Postgres + Redis on the **production mini (`prod`, Tailscale `100.125.64.95`, OS user
`macmini1`)**, with the MacBook Air as the dummy-data dev box. **🔴 steps are
irreversible-ish and gated behind the verified backup (step 3).** Nothing past step 3
runs until the backup restore-verifies.

Machine names are role-based (see `specs/machine-naming.md`): **`prod`** = the app +
real data; **`mcp`** = the LDR + Playwright + MCP-server offload box. The old
`mini1`/`mini2` numbering was retired because the numbers were inverted against the OS
usernames and caused repeated mix-ups.

The supporting tooling is built and locally validated:
- `scripts/backup_prod.sh` — read-only inspect + backup + off-box pull + restore-verify (HARD GATE). `--self-test` proves the machinery on local PG.
- `scripts/provision_prod.sh` — PG16 + Redis, 8GB-tuned, localhost-bound, autostart, idempotent.
- `scripts/etl_sqlite_to_pg.py` — FK-ordered, idempotent SQLite→PG migrator (tested).
- `scripts/seed_dummy.py` — synthetic dev seed for the Air (refuses prod).

---

## 0. ✅ PRECONDITION — SSH access (DONE)

Key-based access to both minis is established over Tailscale and pinned in the Air's
`~/.ssh/config` as role aliases:

```
ssh prod    # macmini1@100.125.64.95  — app + real data
ssh mcp     # macmini2@100.105.113.44  — LDR + Playwright + MCP servers
```

Both verified non-interactive (`ssh prod 'echo OK'` / `ssh mcp 'echo OK'`).
The macOS `LocalHostName`/`ComputerName` rename to `prod`/`mcp` needs `sudo` at each
box (no passwordless sudo) — cosmetic; it does not gate anything below.

## 1. 🔎 Verify non-interactive SSH
`ssh -o BatchMode=yes prod 'echo OK'` → STOP if it prompts or fails.

## 2. 🔎 Read-only inspect prod (decide the source branch)
`scripts/backup_prod.sh` does this automatically (RAM/disk, services, listeners,
existing PG DBs, and whether real data is **Postgres** or **SQLite**). Do NOT guess —
let the script report. **Known from 2026-06-11 inspection:** source is **SQLite**, app
live on `:8000`, no Postgres/Redis yet. ⚠️ There are **two** analytics DBs on prod —
`~/kroger-mcp/data/kroger_analytics.db` and `~/kroger-mcp/kroger_analytics.db` —
confirm which one the running app actually uses (check the app's cwd/`__file__` resolution)
and back up the live one; reconcile/ignore the stray copy.

## 3. 🔴 BACKUP the real data FIRST — the hard gate
```bash
scripts/backup_prod.sh          # SSH_HOST defaults to `prod`
```
- Postgres source → `pg_dump -Fc` + plain SQL + `pg_dumpall --globals-only`.
- SQLite source → `sqlite3 .backup` + `PRAGMA integrity_check`.
- Pulls the backup **off-box** to `data/backups/prod/<TS>/`, checksums it, and
  **restore-verifies** into a throwaway local DB with **exact** per-table `COUNT(*)`
  parity. **MISMATCH → the script aborts. Do not continue.**

## 4. 🔴 Provision PG16 + Redis on prod
```bash
ssh prod 'bash -s' < scripts/provision_prod.sh     # PG_PORT=5433 by default
```
Localhost-bound, scram-sha-256, 8GB tuning, Redis LRU+AOF, both autostart.
If a pg14 already serves :5432, this uses :5433 (never initdb over existing data).
**Confirm it survives a reboot** before relying on it.

## 5. 🔴 Migrate into Postgres + parity
- Set `DATABASE_URL` to the new PG (`postgresql://smartshopper_app:***@localhost:5433/smartshopper`).
- `initialize_pg_database()` builds the 23-table schema (incl. `kroger_tokens`,
  the now user-scoped `deal_watchlist`/`seasonal_patterns`).
- **If source was Postgres**: restore the dump, then apply schema deltas to match
  the current SQLite runtime shape (the port already reconciled these).
- **If source was SQLite** (the confirmed branch): `python scripts/etl_sqlite_to_pg.py`
  (FK-ordered, `ON CONFLICT` idempotent; coerces 0/1→bool, ISO→timestamptz, TEXT→uuid).
- **Parity-check every table count** vs the step-3 source counts. MISMATCH → STOP.

## 6. Local Air dev (dummy data only)
Dedicated `smartshopper_dev` DB + Redis logical DB `/1`; `initialize_pg_database()`
then `python scripts/seed_dummy.py`. Guardrails already in code: root `.env` is
always dev; `_enforce_env_isolation()` refuses a dev boot against a non-local
`DATABASE_URL`; **prod PG is localhost-bound → not LAN-reachable from the Air.**

## 7. Secrets per machine (Keychain)
Per-box gitignored `.env`. On **each** box store in macOS Keychain (Air and `prod`
hold *different* keys): `KROGER_TOKEN_MASTER_KEY`, the PG app-role password, the
Redis `requirepass`. Then `ALTER ROLE smartshopper_app PASSWORD '…'` and set Redis
`requirepass` from those. Verify `git check-ignore .env`.

## 8. Topology
Move the MCP servers (LDR + Playwright + the rest) to the **`mcp`** box; dedicate
`prod` to the app. uvicorn **2 workers** (budget: OS ~1.5G + PG ~1.5G + Redis ~0.6G +
2×uvicorn ~0.6–1G < 5G). App binds `0.0.0.0:8000` (LAN/phone); DB + Redis localhost-only.
Enable the prod firewall; confirm only `:8000` is reachable from the LAN
(`:5433`/`:6379` refused).

## 9. Cutover gate + rollback
**Cutover only when ALL hold:** step-3 backup verified · PG/Redis secured + autostart ·
migration parity matches · per-box env/keys correct · MCP servers healthy on `mcp` ·
a fresh post-cutover backup taken.

**Rollback:** restore from the step-3 backup (`pg_restore` the `.dump`, or drop in
the SQLite `.backup`), re-point `DATABASE_URL` to the prior source, re-verify counts.
Because step 3 pulled the backup **off-box to the Air**, a total loss of `prod` still
leaves a verified copy.

---

### Verification checklist (post-cutover)
- [ ] `prod`: `get_backend()` → `postgresql`; real row counts match step-3 source.
- [ ] `prod`: app reachable on `:8000`; `:5433`/`:6379` refused from the LAN.
- [ ] Air: dummy data only; `APP_ENV=dev`; cannot reach the prod DB.
- [ ] Per-user isolation holds on PG (tokens, watchlist, seasonal — see `tests/test_pg_backend.py`).
- [ ] Fresh post-cutover backup taken + restore-verified.
