# Recipe Detail — Ingredient Row Spacing & Arrangement

## Problem
Each ingredient row is a 7-column grid: drag | qty | unit | name | source-pill | grade-chip | ✕. The source-pill cell holds two children (Manual/Kroger button **and** a long "X cups in pantry" text badge), which makes the cell wrap on the narrow left column of the 2-column layout. When it wraps, the rightmost ✕ button gets pushed below the source pill — visually landing in the bottom-left of the row.

User feedback: "the x's and whatnot get cramped into the bottom left. they looked better on the right. things need to be better spaced and arranged."

## Direction
Keep the teammate's **modeless editing** model (Edit is default, autosave). Quiet the row visually so editing chrome doesn't fight content at rest.

## Acceptance criteria

1. **✕ stays rightmost, single line, never wraps.** Reserve a fixed-width rightmost grid column. ✕ is opacity 0 until row hover; opacity 1 on `[data-ingredient-row]:hover`. Click hit-area unchanged.
2. **Drop the always-visible Manual / Kroger button from every row.** The link state is conveyed by a single tiny status indicator inside the name cell (e.g. a 6px dot or muted icon), with hover/title revealing "Manual" or "Kroger — click to unlink" text. The button still opens the autocomplete; no functional regression.
3. **Pantry status compacts to a single dot inside the name cell** (no more "X cups in pantry" text on each row). Title attribute carries the full text. Color encodes status (ok/low/danger).
4. **Safety grade chip stays as a single letter, but smaller (1.1rem square) and with restrained background.** No bright saturated pill.
5. **Row gap and padding increase.** `gap: 0.75rem`, `padding: 0.65rem 0`. Border line softens to `oklch(94% 0.008 70)`.
6. **Grid simplifies to 5 columns**: `1rem(drag) 3.75rem(qty) 3rem(unit) 1fr(name + inline dots/chip) 1.5rem(✕)`. Column header row matches.
7. **Add-ingredient row** mirrors the same 5-column grid with empty drag/✕ cells so vertical rhythm continues.
8. **No regressions**: autocomplete popover still anchors under name; click-to-edit still works; drag-reorder still works; safety popover still opens; autosave still saves.
9. **Visual smoke test via Playwright**: screenshot both modes; row never wraps at the default desktop viewport; ✕ always at the right edge.
