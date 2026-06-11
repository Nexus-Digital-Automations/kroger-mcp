# `prod` Cutover Runbook — SQLite → Postgres on the production mini

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
