# Spec: Canonical two-machine naming (kill the inverted-number confusion)

## Problem
The two Mac minis were named with `1`/`2` numbers that got **swapped against the OS
usernames**, so every prior session mixed them up (the SSH key repeatedly landed on the
wrong box). Root cause: the machine whose *username* is `macmini1` is the **production**
box, while the machine whose *username* is `macmini2` is the **MCP/offload** box — the
numbers mean the opposite of what they look like.

## Decision
Abandon numbers entirely. Name **by role**: `prod` and `mcp`. Apply the new names at the
Tailscale, macOS-hostname, and ssh-alias layers. **Leave the OS usernames unchanged**
(renaming a macOS account is risky and pointless once the ssh alias carries `User`).

## Canonical mapping (evidence-confirmed 2026-06-11)
| Role | What runs here | Tailscale name | Tailscale IP | OS user (unchanged) |
|------|----------------|----------------|--------------|---------------------|
| **prod** | Smart Shopper app + real data (→ Postgres/Redis/uvicorn) | `prod` | 100.125.64.95 | `macmini1` |
| **mcp**  | LDR + Playwright + the other MCP servers (offload box)   | `mcp`  | 100.105.113.44 | `macmini2` |

Evidence: `prod` has `~/kroger-mcp/data/kroger_analytics.db` + app live on :8000;
`mcp` has the Playwright browser cache and nothing on :8000.

## Acceptance criteria
- [ ] Tailscale MagicDNS name is `prod` on 100.125.64.95 and `mcp` on 100.105.113.44.
- [ ] macOS `ComputerName` + `LocalHostName` are `prod` / `mcp` respectively (via sudo at each box).
- [ ] The Air's `~/.ssh/config` resolves `ssh prod` → macmini1@100.125.64.95 and
      `ssh mcp` → macmini2@100.105.113.44, both key-based, BatchMode, IdentitiesOnly.
- [ ] The old confusing aliases (`mini1`, `mini2`, `prod-mini`, `macmini1`, `macmini2`,
      `macs-mac-mini-1`, `macs-mac-mini-2`) are removed from the Air's ssh config.
- [ ] `ssh prod 'whoami; hostname'` and `ssh mcp 'whoami; hostname'` both succeed non-interactively.
- [ ] Any repo tooling that referenced the old host names is updated to `prod`/`mcp`.

## Out of scope
- Renaming the OS user accounts (`macmini1`/`macmini2`) — deliberately left as-is.
- The Phase-3 migration itself (separate runbook); this spec only fixes naming.
