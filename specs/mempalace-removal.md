# MemPalace Full Removal (both machines)

## Context
Investigation (2026-06-13) found mempalace is net-negative:
- Runs on the 8GB prod mini via SSH (ChromaDB + CPU embeddings) — the direct
  cause of the swap-thrash that paged out Smart Shopper and caused outages.
- 105,160 entries on the mini (767MB) / 26,651 on the laptop (207MB); **96.8%
  in `wing_api`** (raw transcript + tool-dump auto-mining), only ~10 curated.
  854 entries are literal boilerplate (`<command-message>clear</command-message>`).
- Split-brain: hooks mine the LAPTOP store; the MCP queries the MINI store —
  divergent 4×. No identity configured. 260 stale lock files.
- Retrieval returns stale/wrong fragments (searched WEB_WORKERS → got the
  pre-incident "default 2", the opposite of the hard-won lesson).
User decision: remove from both places (not salvageable — plain-text files
+ git already provide better, correct, greppable memory).

## Touchpoints (verified)
- `~/.claude/settings.json`: (a) `mcp__mempalace__*` allow entry; (b) SessionStart
  `mempalace/hooks/mempal_wakeup_hook.sh` block; (c) PreToolUse
  `inject_memory_recall_guidance.py` block (the gate that denies Edit/Write/Task
  until a mempalace search runs — MUST go or edits lock up).
- `~/.claude.json`: top-level `mcpServers.mempalace` (SSH-to-mini stdio server).
- `~/.claude/hooks/inject_memory_recall_guidance.py`: delete (mempalace-only shim).
- `~/.claude/hooks/guidance_rules.py`: excise the self-contained
  `MEMORY_RECALL_RULE` block (L399-493); only importer is the deleted shim.
- `~/.claude/mempalace/` (81MB source repo, holds the wakeup/miner).
- `~/.mempalace/` laptop store (207MB) + mini `~/.mempalace/` (767MB).
- mini `~/.config/mcp/mempalace-run.sh`.
- `uv tool` install `mempalace` on laptop + mini (`~/.local/bin/mempalace*`).
- `~/.claude/CLAUDE.md` "Memory & Learning" section + MCP-tools table row.
- `stop.py`/`pre_compact.py`/`session_end.py`/`subagent_stop.py`: NO real refs
  (earlier counts were `Deter**mine**` false positives) — leave untouched.

## Plan
0. Backup to `~/mempalace-removal-backup-<ts>/`: settings.json, .claude.json,
   guidance_rules.py, inject hook, CLAUDE.md; export curated (non-wing_api)
   drawers from both stores to `curated_salvage.txt`.
1. Stop writers: edit settings.json (3 blocks) + .claude.json (MCP); delete
   inject hook; excise MEMORY_RECALL_RULE.
2. Delete engine+data: `~/.claude/mempalace`, `~/.mempalace`; uv-uninstall
   local; mini: `~/.mempalace`, run script, uv-uninstall.
3. Docs: rewrite CLAUDE.md memory section (drop mempalace; point at plain-text
   files + claude-context for code search).

## Acceptance criteria
- [ ] settings.json & .claude.json are valid JSON, zero "mempalace" matches.
- [ ] `python3 -c "import guidance_rules"` clean; no MEMORY_RECALL_RULE ref left.
- [ ] inject_memory_recall_guidance.py gone; no Edit/Write gate fires.
- [ ] `~/.mempalace` + `~/.claude/mempalace` gone on laptop; `~/.mempalace`
      + run script gone on mini; `which mempalace-mcp` empty on both.
- [ ] CLAUDE.md no longer instructs use of mempalace.
- [ ] Backup dir exists with the 5 config files + curated_salvage.txt.
