# Move guide-type entries out of Recipes into Guides

Three records living in the recipe store (`kroger_recipes.json`) are actually
technique/shopping how-tos, so they show up on the Recipes page when they belong
on the Guides page. Move them into the guide store (`kroger_guides.json`),
converting recipe schema → guide schema with no content lost.

Entries (matched by exact name):
- `Master Guide: Cooking Dry Beans with Soaking`
- `Bean Cooking Starter Kit`
- `Quick-Cooking Lentils & Split Peas (No Soak)`

## Decisions
- [x] Move all three guide-type entries (not just the Master Guide).
- [x] Conversion = faithful flatten: markdown `instructions` → plain-text step
      list; structured `ingredients` (incl. Kroger product_ids) folded into a
      labeled "INGREDIENTS / SHOPPING LIST" section so nothing is lost.
- [x] Apply to BOTH the live prod data on mini #1 AND the local copy, backing up
      both JSON files first (files are live-state / gitignored / not deployed).
- [x] Match entries by exact name (IDs differ between local and prod); migration
      is idempotent (re-run does not duplicate).
- [x] Restart the prod web app after editing so the running process reloads.

## Acceptance Criteria
- [x] None of the three names remain in `kroger_recipes.json` (local) — recipes count drops by exactly 3.
      verify: cmd python3 scripts/verify_guide_migration.py kroger_recipes.json kroger_guides.json
- [x] Three guides with those exact names exist in `kroger_guides.json` (local).
      verify: cmd python3 scripts/verify_guide_migration.py kroger_recipes.json kroger_guides.json
- [x] Each migrated guide preserves the original description, carries the recipe's tags, has a non-empty `steps` list, and folds every ingredient (with product_id/override) into steps.
      verify: cmd python3 scripts/verify_guide_migration.py kroger_recipes.json kroger_guides.json
- [x] Backups of both JSON files exist under output/guide-migration/ before any write.
      manual: output/guide-migration/
- [x] The same migration is applied to prod (mini #1) and verified there.
      manual: output/guide-migration/prod-verify.txt
- [x] Both JSON files remain valid JSON in the JsonStore shape (indent=2) after migration.
      verify: cmd python3 scripts/verify_guide_migration.py kroger_recipes.json kroger_guides.json
