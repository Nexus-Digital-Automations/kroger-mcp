# UI Polish — Editorial / Culinary aesthetic, site-wide

## Why

The recipe-editor feature shipped functional but visually rough: ✕ glyphs at full opacity, abrupt hover states, inconsistent close buttons across modals, drag handles that compete with content, toasts and pills that don't share a hover language. The user's instruction: make it more aesthetic and slick — especially the ✕'s and deletions.

## Direction (user-selected)

- **Scope**: all inline-editor surfaces site-wide (recipe detail, recipes index, pantry, shopping list, favorites, meal plan, macros).
- **Delete behavior**: hide-until-row-hover; hover the icon shows a soft red wash + tooltip.
- **Aesthetic**: editorial / culinary — refined and warm. Lean into Lora serif + oklch cream palette, softer borders, generous whitespace.
- **Motion**: purposeful only — 150–250ms ease-out; every transition has a job.

## Acceptance criteria

1. A single shared `.ss-row-delete` class (in `base.html`) gives every row-level delete the same hide-until-hover behavior + soft red wash on icon hover + 160ms ease-out fade. Existing `.ss-row-action.is-danger` becomes an alias of this class so I don't fragment the API.
2. Every `✕` text glyph in templates is replaced by a single SVG icon component (currentColor, 12×12, stroke 2). Looks crisp at any zoom.
3. Modal close buttons (`recipe_add_modal`, meal panel, chat) all use a shared `.ss-modal-close` style: 32×32 circular, neutral-on-cream by default, subtle scale on hover.
4. Tag pill `×` uses the same icon component, slightly smaller, inherits color from the pill.
5. Confirm dialogs (`window.confirm`) on destructive flows are replaced where the existing code has an undo path; remaining `confirm()` calls keep `window.confirm` but with consistent copy ("Delete X?" vs "Remove X?" inconsistencies).
6. Drag handles use the same SVG-icon component (a grip-vertical-dots, not the `⋮⋮` Unicode characters which look uneven on different fonts).
7. The recipe-detail saved-pulse animation is calmed: shorter (350ms), warmer hue (oklch terra rather than green), more subtle.
8. Whole-repo lint / format remains clean.

## Critical files

- `src/kroger_mcp/web/templates/base.html` — owns the shared `.ss-row-delete`, `.ss-modal-close`, `.ss-icon-btn` styles and the inline SVG sprite (`<svg><symbol>` for x-mark, grip-dots, plus-mark).
- `src/kroger_mcp/web/templates/recipe_detail.html` — swap the inline ✕ + ⋮⋮ for shared icon refs; tone the saved-pulse.
- `src/kroger_mcp/web/templates/recipes.html`, `shopping_list.html`, `pantry.html`, `favorites.html`, `meal_plan.html` — adopt shared classes where row-level deletes exist.
- `src/kroger_mcp/web/templates/_macros/action_menu.html`, `_macros/recipe_add_modal.html` — modal close + danger menu items use shared styles.

## Verification

- `bash scripts/lint.sh` clean (ruff, black, eslint, prettier).
- Visit `/recipes/<id>` and confirm: ✕ on ingredient rows + step rows hidden until hover; hovering ✕ shows red wash + tooltip; drag handle is a clean grip icon; tag × is consistent; saved-pulse is warm and brief.
- Visit `/shopping-list`, `/pantry`, `/meal-plan`, `/recipes` and confirm the same delete language.
- Mobile (375px): no overflow; tap targets stay ≥32px.

## Tasks

- [ ] Add shared CSS (`.ss-row-delete`, `.ss-modal-close`, `.ss-icon-btn`) + SVG icon sprite to base.html
- [ ] Replace ✕ glyphs in recipe_detail.html with shared SVG icon + row-hover behavior
- [ ] Replace ⋮⋮ drag handle with crisp SVG grip-dots icon
- [ ] Tone the saved-pulse animation (shorter, warmer)
- [ ] Polish modal close buttons across recipe_add_modal, meal panel, chat widget
- [ ] Polish tag pill × to share icon + color inheritance
- [ ] Apply shared delete pattern across shopping_list / pantry / favorites / meal_plan / recipes index
- [ ] Run lint / format suite repo-wide; fix any new issues
