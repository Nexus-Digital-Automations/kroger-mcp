/*
 * Smart ingredient linking — shared client helper.
 *
 * Owns the one network call the recipe linking popover makes for "your usuals":
 * GET /api/ingredients/suggest. Kept as a tiny, framework-free global so the
 * (still duplicated) Alpine `ingredientPanel` component in recipe_view.html and
 * recipe_edit.html both call the exact same code instead of forking a fetch.
 *
 * Response shape (account-scoped on the server):
 *   { canonical_name, canonical_confidence, best_guess, suggestions: [
 *       { product_id, product_description, score, reason, times_linked } ] }
 *
 * Never throws: on any failure it resolves to an empty, well-formed result so
 * the popover degrades to plain live Kroger search.
 */
(function () {
  'use strict';

  const EMPTY = Object.freeze({
    canonical_name: null,
    canonical_confidence: null,
    best_guess: null,
    suggestions: [],
  });

  async function fetchSuggestions(name, limit) {
    const q = (name || '').trim();
    if (!q) return EMPTY;
    const lim = Number.isFinite(limit) ? limit : 6;
    try {
      const r = await fetch(
        '/api/ingredients/suggest?name=' +
          encodeURIComponent(q) +
          '&limit=' +
          encodeURIComponent(lim)
      );
      if (!r.ok) return EMPTY;
      const d = await r.json();
      return {
        canonical_name: d.canonical_name || null,
        canonical_confidence: d.canonical_confidence ?? null,
        best_guess: d.best_guess || null,
        suggestions: Array.isArray(d.suggestions) ? d.suggestions : [],
      };
    } catch (_) {
      return EMPTY;
    }
  }

  window._ssSmartLink = { fetchSuggestions };
})();
