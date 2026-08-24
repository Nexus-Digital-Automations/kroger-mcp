# Production runbook — Smart Shopper on Mac mini #1

Verified live on 2026-08-23. Every command below was run against the real host.

## Topology

The MCP server does **not** run on the laptop. `.mcp.json` opens an SSH session to the
mini and runs the server *there*, speaking stdio back over the pipe. The laptop is a
thin client; prod is the only place the code actually executes.

```
laptop  ──ssh──▶  macmini1@100.125.64.95
                    ~/.config/mcp/kroger-run.sh
                      └─ cd ~/kroger-mcp && exec uv run --frozen kroger-mcp
```

| Component | Identity | State |
|---|---|---|
| MCP server | spawned per session by `kroger-run.sh` | on-demand, not a daemon |
| Web app | `com.smartshopper.web` (launchd) | port 8000, `HTTP 302` when healthy |
| Postgres | `homebrew.mxcl.postgresql@16` | **port 5433**, db **`smartshopper`**, 16.14, 38 tables |
| Redis | `homebrew.mxcl.redis` | `PING` → `PONG` |
| Deal scanner | `com.user.kroger-discount-scanner` | daily at **09:00**; logs to `/tmp/kroger-scanner-{out,err}.log` |

LaunchAgents live in `~/Library/LaunchAgents/` on the mini.

## Health check

Full functional check — invokes all 156 actions over the real `.mcp.json` transport, in
preview/dry-run mode only (no live cart, pantry, or recipe mutations):

```bash
uv run --frozen python scripts/smoke_mcp.py --restart
```

Expect **82 PASS / 5 FAIL / 69 SKIP**. The 5 known failures are 3× `auth.*` (needs
interactive re-auth) and 2× `notion.*` (`NOTION_API_KEY` unset). Anything beyond those is
new. Results checkpoint to `output/mcp-smoke/results.jsonl` after every call, so an
interrupted run resumes — omit `--restart` to continue one. Full analysis:
`output/mcp-smoke/report.md` — note `output/` is gitignored, so that file is local to the
machine that ran the sweep; the durable summary lives in
`plans/verify-mcp-full-feature-surface.md`.

Fast infrastructure check, no MCP round-trip:

```bash
ssh -o BatchMode=yes -o IdentityFile=~/.ssh/mini_offload_ed25519 \
    macmini1@100.125.64.95 -- '
  launchctl list | grep -Ei "smartshopper|postgres|redis|kroger"
  curl -s -o /dev/null -w "web -> HTTP %{http_code}\n" http://localhost:8000/
  /opt/homebrew/bin/redis-cli ping
  /opt/homebrew/opt/postgresql@16/bin/psql -p 5433 -d smartshopper -tAc "select 1"
'
```

`launchctl list` prints `-` in the PID column for the scanner. That is **correct** — it is
a scheduled job, not a resident daemon.

## Deploying

`git push` to `origin/main` **is a production deploy.** The `pre-push` hook runs
`Automation Agent/scripts/deploy-smartshopper-to-mini.sh`, which rsyncs code, runs
`uv sync --frozen`, and `launchctl kickstart -k`s the web app.

Live state is excluded from the sync and is never clobbered: `.env*`, `.kroger_token*`,
`kroger_cart.json`, `kroger_recipes.json`, `kroger_guides.json`,
`kroger_order_history.json`, `kroger_analytics.db`, and `data/`.

Manual deploy (identical to what the hook runs):

```bash
sh "/Users/jeremyparker/Desktop/Claude Coding Projects/Automation Agent/scripts/deploy-smartshopper-to-mini.sh"
```

### Three deploy gotchas

1. **A failed deploy does not fail the push.** The hook logs
   `[pre-push] deploy FAILED — push continues; deploy manually if needed` and exits 0.
   Always read the push output; a green `git push` is not evidence that prod updated.
2. **`uv sync` errors are swallowed** (`|| echo "[deploy] uv sync warned (continuing)"`).
   The app restarts regardless, so a dependency change can silently fail to install and
   leave prod running against stale packages. After any dependency change, verify on the
   mini directly.
3. **rsync runs without `--delete`.** Files deleted locally persist on the mini forever.
   Remove them by hand when it matters.

Also note the hook itself lives in `.git/hooks/pre-push`, which is **not version
controlled** — a fresh clone has no auto-deploy until you reinstall it via
`Automation Agent/scripts/install-smartshopper-deploy-hook.sh`. And `docs/` is excluded
from the rsync, so this runbook exists only on the laptop.

## Restore procedures

**Web app down** (port 8000 not answering):

```bash
ssh -o BatchMode=yes -o IdentityFile=~/.ssh/mini_offload_ed25519 macmini1@100.125.64.95 -- \
  'launchctl kickstart -k "gui/$(id -u)/com.smartshopper.web"'
```

**Postgres or Redis down:**

```bash
ssh ... macmini1@100.125.64.95 -- '/opt/homebrew/bin/brew services restart postgresql@16'
ssh ... macmini1@100.125.64.95 -- '/opt/homebrew/bin/brew services restart redis'
```

**MCP session won't start** — re-run the launcher by hand and read the output. A clean
start prints the FastMCP banner and `Starting MCP server 'Kroger API Server'`:

```bash
ssh -o BatchMode=yes -o IdentityFile=~/.ssh/mini_offload_ed25519 macmini1@100.125.64.95 -- \
  /Users/macmini1/.config/mcp/kroger-run.sh
```

A mid-session disconnect that recovers on re-run is transport-transient, not a server
fault — this was observed and confirmed during verification.

**Kroger auth expired** (`auth.*` returning `Authentication required`): run
`auth(action='start')`, open the URL, approve, then pass the full redirect URL back to
`auth(action='complete', redirect_url=...)`. The PKCE verifier is written to
`/tmp/kroger_mcp_auth_state.json` **on the mini**, so it survives the MCP server exiting
between the two steps — but it is a single slot, so never begin a second auth flow before
finishing the first. The redirect lands on `http://localhost:8000/callback` with nothing
listening; the browser error is expected, just copy the URL.

## Two traps worth knowing

**Non-interactive SSH has no Homebrew in `PATH`.** `psql`, `redis-cli`, and `brew` all
fail with `command not found` over `ssh host -- cmd`. Use absolute paths
(`/opt/homebrew/opt/postgresql@16/bin/psql`, `/opt/homebrew/bin/redis-cli`) — every
command in this runbook already does.

**Postgres is stricter than SQLite, and tests run on SQLite.** Three of the four defects
found during verification were queries that passed the whole test suite and then failed
only against prod: a bare column under `GROUP BY`, and `MAX()` over a `BOOLEAN`. A green
`pytest` run is *not* evidence that a query works on prod. Run new SQL against the real
database before shipping it:

```bash
ssh ... macmini1@100.125.64.95 -- \
  '/opt/homebrew/opt/postgresql@16/bin/psql -p 5433 -d smartshopper -c "<query>"'
```
