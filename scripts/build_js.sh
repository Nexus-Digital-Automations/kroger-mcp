#!/usr/bin/env bash
# Minify the hand-written shared JS bundles (Alpine ships pre-minified already).
#
# Run after editing any of the source files below, then commit the regenerated
# *.min.js. Prod serves the committed minified files — no node toolchain on the
# server. terser keeps top-level/global and property names (window._ss* exports,
# Alpine component registrations) untouched; it only mangles local variables.
set -euo pipefail
cd "$(dirname "$0")/.."

JS_DIR="src/kroger_mcp/web/static/js"
FILES=(action_menu ingredient_panel linker_popover)

for name in "${FILES[@]}"; do
  npx -y terser@5.31.6 \
    "$JS_DIR/$name.js" \
    --compress \
    --mangle \
    --output "$JS_DIR/$name.min.js"
  echo "Built $JS_DIR/$name.min.js ($(wc -c < "$JS_DIR/$name.min.js" | tr -d ' ') bytes)"
done
