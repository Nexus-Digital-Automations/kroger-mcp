/**
 * Tailwind v3 config for the pre-built stylesheet (replaces the Play CDN).
 *
 * Build: scripts/build_css.sh → src/kroger_mcp/web/static/css/tailwind.css
 * (committed to git so prod needs no node toolchain).
 *
 * After editing templates/JS with NEW Tailwind classes, re-run the build and
 * commit the regenerated CSS. Dynamically-constructed class names the content
 * scan can't see must be added to `safelist` as literal strings.
 */
module.exports = {
  content: [
    './src/kroger_mcp/web/templates/**/*.html',
    './src/kroger_mcp/web/static/js/**/*.js',
  ],
  theme: {
    extend: {
      // Migrated from the inline tailwind.config in base.html (Play CDN era).
      fontFamily: {
        sans: ['DM Sans', 'sans-serif'],
        serif: ['Lora', 'serif'],
      },
    },
  },
  safelist: [],
};
