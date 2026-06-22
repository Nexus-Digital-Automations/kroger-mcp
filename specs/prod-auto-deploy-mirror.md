# prod auto-deploy: rely on the existing push-triggered rsync deploy

prod (`Macs-Mac-mini-2`, tailnet `prod` = `macmini1@100.125.64.95`) appeared to
"not mirror" because nothing had pushed `main` to origin — the deploy is
push-triggered, and only a feature branch had been pushed.

## How it actually works (discovered, not built)

- Laptop git hook `.git/hooks/pre-push` fires on `main` → `origin`.
- It runs `Automation Agent/scripts/deploy-smartshopper-to-mini.sh`, which
  `rsync`s the laptop working tree to `~/kroger-mcp/` on prod — excluding
  `.git`, `node_modules`, `.venv`, caches, `logs/`, `docs/`, and all live state
  (`.env*`, tokens, cart/recipes/guides JSON, analytics DB, `data/`) — then runs
  `uv sync --frozen` and `launchctl kickstart -k com.smartshopper.web`.
- rsync runs WITHOUT `--delete`, so prod-only files (`run-web.sh`, live state)
  are never removed. The deploy ignores prod's `.git` entirely, so prod's git
  state is not load-bearing.

So: **commit → push main to origin → prod auto-deploys + restarts.** That is the
"automatic" path; no extra infrastructure is added.

## Decisions

- [x] Use the existing pre-push rsync deploy as the auto-deploy; do NOT add a parallel scheduled git-pull mirror (would be redundant and fight the rsync deploy).
- [x] Tidy prod's git safely with `reset --mixed origin/main` (moves HEAD/index to origin/main, leaves the working tree and all prod-only/untracked files intact — no deletions). prod's git is cosmetic, not load-bearing.

## Acceptance Criteria

- [x] The favorites merged-column change is on `origin/main` (fast-forwarded, not a side branch).
  - verify: cmd: cd "/Users/jeremyparker/Desktop/Claude Coding Projects/Smart Shopper" && git fetch origin -q && git show origin/main:src/kroger_mcp/web/templates/favorites_detail.html | grep -qE ">Qty</th>" && ! git show origin/main:src/kroger_mcp/web/templates/favorites_detail.html | grep -q ">Ordered</th>"
- [x] The pre-push deploy hook is present and points at the real deploy script.
  - verify: present: .git/hooks/pre-push
- [x] The push of main actually deployed: prod serves the merged Qty column (single column, no "Ordered") and the web service responds.
  - manual: prod working-tree favorites_detail.html has no Ordered th; curl / returns non-000
- [x] prod git HEAD == origin/main after the safe tidy, with run-web.sh still present on disk and node_modules preserved (now untracked).
  - manual: prod git rev-parse HEAD == origin/main; ls run-web.sh; node_modules present
- [x] A future push to origin/main reaches prod with no manual steps (mechanism verified by this push having deployed).
  - manual: confirmed via the live serve check above
