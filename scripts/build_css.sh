#!/usr/bin/env bash
# Build the committed Tailwind stylesheet (replaces the Play CDN).
#
# Run after any template/JS change that adds NEW Tailwind utility classes,
# then commit the regenerated tailwind.css. Prod serves the committed file —
# no node toolchain on the server. Version pinned to Tailwind v3 (same class
# semantics as the Play CDN this replaced).
set -euo pipefail
cd "$(dirname "$0")/.."

npx -y tailwindcss@3.4.17 \
  -c tailwind.config.js \
  -i src/kroger_mcp/web/static/css/tailwind.source.css \
  -o src/kroger_mcp/web/static/css/tailwind.css \
  --minify

echo "Built src/kroger_mcp/web/static/css/tailwind.css ($(wc -c < src/kroger_mcp/web/static/css/tailwind.css | tr -d ' ') bytes)"
