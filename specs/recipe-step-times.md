# Recipe Step Times & Total Time

## Goal
Per-step time blocks and a total recipe time, visible everywhere recipes appear,
so the user can answer "what can I make in 30 minutes tonight?" and see how
heavy a cooking day is.

## Approved scope (AskUserQuestion 2026-06-12)
- **Capture: auto-detect + override.** A Python parser extracts durations
  already written in step text ("simmer 20 minutes", "bake 1 hour",
  "marinate overnight") so all existing recipes get times with zero data
  entry. Per-step explicit overrides are stored on the recipe.
- **Total: derived + editable.** Suggested total = sum of step times, split
  into active vs passive (hands-off). Recipe gains optional explicit
  `prep_time_minutes` / `cook_time_minutes` / `total_time_minutes` fields;
  explicit total wins over the derived one when set.
- **Display: all four surfaces.** Recipe view (per-step chips + header
  summary), recipe cards (+ "Quickest" sort), meal-plan calendar entries,
  and an active-vs-passive split ("30 min active · 8 h hands-off").

## Design decisions
- **Single parser, Python only** (`src/kroger_mcp/tools/step_times.py`).
  The browser never re-implements extraction: the recipe page embeds
  server-computed annotations aligned to the flat step list, and the
  instructions editor refetches `GET /api/recipes/{id}/step-times` after any
  step mutation or override change. Server pre-formats labels.
- **Overrides keyed by content hash** — `recipe["step_times"]` maps
  `sha1(normalized step text)[:12]` → `{minutes, passive}`. Survives
  reordering; editing a step's text intentionally drops its override (the
  text change likely changed the time). No schema migration: `instructions`
  stays the text/JSON-array source of truth, MCP write paths untouched.
- **Per-step value = MAX duration found in the step** (not sum): "bake 20
  minutes, rotating after 10" → 20. Ranges take the upper bound ("5 to 7
  minutes" → 7). "overnight" → 480 min passive. Matches > 48 h ignored.
- **Passive detection** by verb list (marinate, rest, chill, rise, proof,
  refrigerate, freeze, soak, brine, steep, ferment, cure, sit, stand, cool,
  overnight). Passive steps style differently and total separately.
- **Derived total = active sum + passive sum**, reported as the pair.
  Card/calendar chips show the effective total (explicit wins), with passive
  noted where space allows.

## Changes
1. NEW `src/kroger_mcp/tools/step_times.py`: `extract_step_time(text)`,
   `step_time_key(text)`, `annotate_steps(flat_steps, overrides)`,
   `recipe_time_summary(recipe)` (parses instructions text directly, for
   list/calendar use), `format_minutes(m)`.
2. `web/routes/recipes.py`: `_build_recipe_context` adds `step_time_data`
   (aligned annotations + totals); `_recipes_payload` adds
   `time_total/time_active/time_passive` to `recipes_json`.
3. `web/routes/api/recipes.py`: `UpdateRecipeBody` gains the three time
   fields (≤0 clears) + `step_times` merge map (null value deletes a key);
   NEW `GET /api/recipes/{id}/step-times`.
4. `templates/recipe_view.html` + `recipe_edit.html` (identical edits):
   step-row time chips (auto = subtle "~", override = solid; passive =
   hands-off style), edit-mode chip popover (minutes + hands-off checkbox +
   save/clear → PATCH), header time summary line, edit-mode prep/cook/total
   inputs, editor refetches annotations after step mutations.
5. `templates/recipes.html`: card time chip; sortRank option
   `{v:'time', l:'Quickest', defaultDir:'asc'}` (missing times sort last).
6. `web/routes/meal_plan.py` + `templates/meal_plan.html`: calendar entries
   carry recipe total time; chip per entry.

## Acceptance criteria
- [ ] Unit: extractor handles min/hour/ranges/overnight/passive verbs/
      multi-duration-max/no-time/temperature non-matches; override merge;
      totals math; formatting (`45 min`, `1 h 10 min`, `8 h`).
- [ ] A recipe whose steps mention durations shows chips and a header total
      with zero manual input.
- [ ] Setting an override changes the chip + totals and persists across
      reload; clearing reverts to auto; editing step text drops its override.
- [ ] Explicit total overrides derived total everywhere displayed.
- [ ] /recipes cards show time and "Quickest" sort orders by effective total.
- [ ] Meal-plan calendar entries show the recipe's time.
- [ ] Full pytest + e2e green, ruff/mypy clean, CSS rebuilt, deployed to prod
      gracefully with stability probes.
