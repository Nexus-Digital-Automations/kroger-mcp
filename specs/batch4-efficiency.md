# Batch 4 — Efficiency Round 3 (acceptance criteria)

## Context
Follow-up to Batch 3. Cut redundant per-request disk/parse/compute and remaining
full-page reloads on the single-worker (WEB_WORKERS=1) 8 GB prod mini. Three
approved bundles: hot-path caching, cache costly analytics, frontend network.
Full design: `~/.claude/plans/okay-please-plan-out-imperative-feather.md`.

## Acceptance criteria

### Bundle 1 — hot-path caching
- [ ] `_load_recipes()` returns identical data but skips disk read on the 2nd+
      call when `kroger_recipes.json` is unchanged (fingerprint = mtime_ns+size).
- [ ] Mutating the dict returned by `_load_recipes()` does NOT change what a
      later `_load_recipes()` returns (no-poison contract preserved).
- [ ] Writing recipes (`_recipes_store.save`) invalidates the cache (next load
      reflects the change).
- [ ] `templating.py` builds an explicit Jinja `Environment` with a
      `FileSystemBytecodeCache`; `auto_reload` is False when `APP_ENV=prod`,
      True otherwise. All existing pages still render.

### Bundle 2 — analytics caching
- [ ] A 2nd `GET /api/deals/auto` with the same location+min_savings within the
      TTL issues ZERO new Kroger searches (served from Redis).
- [ ] With Redis unavailable (`get_redis()→None`), `auto_deals` still returns
      correct deals (uncached, no error).
- [ ] `get_upcoming_holidays` returns cached results within a day; the cache key
      rolls over at midnight (date in key).
- [ ] Prediction results are cached at the dict boundary (no dataclass
      serialisation failure; cache actually populates).

### Bundle 3 — frontend network
- [ ] Switching meal plans via the selector updates the calendar + URL WITHOUT a
      full document navigation (region swap + pushState).
- [ ] Products "view details" re-runs the search in-page WITHOUT a full
      navigation.
- [ ] `base.html` preloads the Google Fonts CSS; 3 JS files ship minified and are
      referenced as `*.min.js` (behavior unchanged).

### Quality gates
- [ ] ruff + mypy clean (zero errors).
- [ ] New pytest cases (recipes cache, auto_deals cache, holidays cache) pass.
- [ ] Playwright: meal-plan/products no-full-nav sentinels pass; deals render.
- [ ] Deployed gracefully (no `kickstart -k`), WEB_WORKERS=1, assets serve 200,
      good-neighbor uptime intact.
