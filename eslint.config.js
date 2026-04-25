// ESLint v9 flat config. Owns: lint rules for runtime JS (web/static/js) and Playwright tests.
// Does NOT own: Python lint (see pyproject.toml [tool.ruff/black/mypy]) or Jinja templates.
// Called by: scripts/lint.sh, package.json "lint" script.
//
// Self-contained — no @eslint/js dependency so this works without npm install.
// Rules below are the subset of eslint:recommended that catches real bugs in this codebase.

const browserGlobals = {
  Alpine: 'readonly',
  $el: 'readonly',
  $dispatch: 'readonly',
  $nextTick: 'readonly',
  $watch: 'readonly',
  $refs: 'readonly',
  $root: 'readonly',
  $store: 'readonly',
  window: 'readonly',
  document: 'readonly',
  fetch: 'readonly',
  console: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly',
  localStorage: 'readonly',
  sessionStorage: 'readonly',
  navigator: 'readonly',
  location: 'readonly',
  CustomEvent: 'readonly',
  Event: 'readonly',
  URLSearchParams: 'readonly',
  FormData: 'readonly',
  AbortController: 'readonly',
  getComputedStyle: 'readonly',
};

const nodeGlobals = {
  require: 'readonly',
  module: 'readonly',
  process: 'readonly',
  console: 'readonly',
  __dirname: 'readonly',
  __filename: 'readonly',
  Buffer: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly',
  global: 'readonly',
};

// Curated rule set — deliberately small. Add rules only for bug-class violations,
// not style. Prettier owns formatting.
const bugRules = {
  'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
  'no-undef': 'error',
  'no-redeclare': 'error',
  'no-dupe-keys': 'error',
  'no-dupe-args': 'error',
  'no-unreachable': 'error',
  'no-constant-condition': ['error', { checkLoops: false }],
  'no-empty': ['error', { allowEmptyCatch: true }],
  'no-cond-assign': ['error', 'except-parens'],
  'no-self-assign': 'error',
  'use-isnan': 'error',
  'valid-typeof': 'error',
};

module.exports = [
  {
    ignores: [
      'node_modules/**',
      '.cache/**',
      '.validation-artifacts/**',
      '.playwright-mcp/**',
      '.venv/**',
      'src/kroger_mcp/web/templates/**',
      '**/*.min.js',
    ],
  },
  {
    files: ['src/kroger_mcp/web/static/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals: browserGlobals,
    },
    rules: bugRules,
  },
  {
    // Playwright tests are Node, but `page.evaluate(() => …)` callbacks execute in browser
    // context. ESLint can't distinguish the two scopes, so allow both global sets here.
    files: ['tests/playwright/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'commonjs',
      globals: { ...nodeGlobals, ...browserGlobals },
    },
    rules: bugRules,
  },
];
