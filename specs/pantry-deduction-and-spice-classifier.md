# Pantry deduction + layered spice classifier

Fix two broken systems: (1) meal-plan recipes never deduct pantry levels, and
(2) the spice classifier recognizes too few spices. See the approved plan for
full context.

## Acceptance Criteria
- [x] reconcile_past_meals auto-deducts a strictly-past planned meal exactly once and is idempotent on a second call
  verify: present reconcile_past_meals src/kroger_mcp/analytics/meal_planning.py
- [x] Reconciliation is wired into shopping-list generation and the /week route (no scheduler)
  verify: present reconcile_past_meals src/kroger_mcp/web/routes/api/meal_plan.py
- [x] _deduct_ingredients resolves typed-name ingredients via the fuzzy matcher before skipping
  verify: present match_ingredient_to_pantry src/kroger_mcp/analytics/meal_planning.py
- [x] match_ingredient_to_pantry is user-scoped (accepts and forwards user_id)
  verify: present user_id=user_id src/kroger_mcp/analytics/recipe_integration.py
- [x] Shopping-list generation excludes already-cooked/deducted meals
  verify: present exclude_cooked src/kroger_mcp/analytics/meal_planning.py
- [x] Undo of a past meal sets cook_skipped so reconcile never silently re-deducts
  verify: present cook_skipped src/kroger_mcp/analytics/meal_planning.py
- [x] cook_skipped column exists in both SQLite and Postgres meal_entries schemas
  verify: present cook_skipped src/kroger_mcp/analytics/pg_database.py
- [x] classify_spice layered classifier exists and is_spice delegates to it
  verify: present classify_spice src/kroger_mcp/analytics/ingredients.py
- [x] category_type is cached from Kroger aisle data during product metadata upsert
  verify: present category_type_from_aisles src/kroger_mcp/tools/product_catalog.py
- [x] Shopping list uses the authoritative category_type signal for the spice split
  verify: present classify_spice src/kroger_mcp/web/routes/api/shopping_list.py
- [x] New pantry reconcile tests pass
  verify: tests tests/test_meal_reconcile.py
- [x] All spice tests pass, including every pre-existing negative case
  verify: tests tests/unit/test_is_spice.py
- [x] ruff is clean on src/
  verify: cmd ruff check src/kroger_mcp/analytics/meal_planning.py src/kroger_mcp/analytics/ingredients.py

## Decisions
- [x] Auto-deduct fires via lazy reconciliation on view/shopping-list, not a background scheduler
- [x] A meal is auto-deducted only when its date is strictly before today (meals dated today are not)
- [x] Deduction matches exact product_id first, then canonical/fuzzy name, skipping only when no match
- [x] Undo of a past meal is permanent via a cook_skipped tombstone column
- [x] Spice classification trusts Kroger aisle/category when linked, else expanded lexicon + narrow blend regex
- [x] All existing test_is_spice negative cases remain non-spice (fresh garlic, onion, bell pepper, butter, oil, tomato)
