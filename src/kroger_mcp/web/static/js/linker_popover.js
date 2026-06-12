/**
 * Kroger product-linking popover — Alpine MIXIN for ingredientPanel.
 *
 * Ownership: the ac* state machine behind
 * templates/_macros/ingredient_linker_popover.html — live product search
 * (/api/products/search), the account's "your usuals" suggestions
 * (/api/ingredients/suggest), lazy product detail, popover anchoring, and
 * selection/link actions. Split from ingredient_panel.js (one cohesive
 * component, two files for size) — spread into the component object via
 * `...window._ssLinkerPopoverMixin()`, so `this` here IS the panel
 * (this.ings / this.linkProduct / this._putIngredientsNow).
 *
 * Load order: this file must load BEFORE ingredient_panel.js.
 */
window._ssLinkerPopoverMixin = () => ({
  // ── Product autocomplete (inline) ────────────────────────────────
  // Search /api/products/search with a 250ms debounce. Results carry
  // safety enrichment we re-use to colour the row chip on selection.
  // detailCache survives across acOpenFor/acClose cycles intentionally —
  // re-expanding a previously-seen product should not re-fetch.
  // anchorPos is {left, top} in pixels relative to #ingredients-card;
  // the popover lives as a child of that card and reads anchorPos via
  // acPopoverStyle. Anchor element is stored unreactively (_anchorEl)
  // so the DOM ref doesn't end up in Alpine's proxy machinery.
  acState: { idx: null, query: '', results: [], active: 0, loading: false, debounce: null,
             expanded: null, detailCache: {}, detailLoading: {}, detailError: {},
             anchorPos: { left: 0, top: 0 } },
  // Smart "your usuals" state — account-scoped suggestions, the learned
  // canonical name, and the pre-selected best guess. Populated by
  // _acFetchSuggestions via /api/ingredients/suggest (ingredient_linker.js).
  acSuggest: { items: [], best: null, canonical: null, canonicalConf: null, loading: false },
  _anchorEl: null,
  _onAcReposition: null,
  _onAcOutside: null,
  get acPopoverStyle() {
    const p = this.acState.anchorPos || { left: 0, top: 0 };
    return `left:${p.left}px; top:${p.top}px;`;
  },
  _acReposition() {
    if (!this._anchorEl) return;
    const card = document.getElementById('ingredients-card');
    if (!card) return;
    const a = this._anchorEl.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    // Anchor under the bottom-left of the name cell, +4px gap.
    this.acState.anchorPos = { left: Math.round(a.left - c.left), top: Math.round(a.bottom - c.top + 4) };
  },
  acOpenFor(idx, seed, anchorEl) {
    const cache = this.acState.detailCache || {};
    this._anchorEl = anchorEl || null;
    this.acState = { idx, query: seed || '', results: [], active: 0, loading: false, debounce: null,
                     expanded: null, detailCache: cache, detailLoading: {}, detailError: {},
                     anchorPos: { left: 0, top: 0 } };
    this._acReposition();
    // Recompute on scroll/resize so the popover tracks its anchor.
    if (!this._onAcReposition) {
      this._onAcReposition = () => this._acReposition();
      window.addEventListener('scroll', this._onAcReposition, true);
      window.addEventListener('resize', this._onAcReposition);
    }
    // Close on pointerdown OUTSIDE the popover/name cell. Focus can now live
    // in the popover's own search input, where the name cell's blur-close
    // can't see it — without this, clicking elsewhere would leave the popover
    // stranded open.
    if (!this._onAcOutside) {
      this._onAcOutside = (ev) => {
        if (ev.target.closest('.ss-autocomplete-popover') || ev.target.closest('.ing-name-cell')) return;
        this.acClose();
      };
      document.addEventListener('pointerdown', this._onAcOutside, true);
    }
    if (this.acState.query.trim().length >= 2) this._acSearch(this.acState.query);
    // Pull this account's "usuals" regardless of seed length — a short or
    // empty seed still has a best guess worth pre-selecting.
    this.acSuggest = { items: [], best: null, canonical: null, canonicalConf: null, loading: true };
    this._acFetchSuggestions(idx, this.acState.query);
  },
  acClose() {
    const cache = this.acState.detailCache || {};
    this.acState = { idx: null, query: '', results: [], active: 0, loading: false, debounce: null,
                     expanded: null, detailCache: cache, detailLoading: {}, detailError: {},
                     anchorPos: { left: 0, top: 0 } };
    this.acSuggest = { items: [], best: null, canonical: null, canonicalConf: null, loading: false };
    this._anchorEl = null;
    if (this._onAcReposition) {
      window.removeEventListener('scroll', this._onAcReposition, true);
      window.removeEventListener('resize', this._onAcReposition);
      this._onAcReposition = null;
    }
    if (this._onAcOutside) {
      document.removeEventListener('pointerdown', this._onAcOutside, true);
      this._onAcOutside = null;
    }
  },
  acType(text) {
    this.acState.query = text;
    if (this.acState.debounce) clearTimeout(this.acState.debounce);
    const q = (text || '').trim();
    if (q.length < 2) { this.acState.results = []; this.acState.expanded = null; return; }
    this.acState.debounce = setTimeout(() => this._acSearch(q), 250);
  },
  async _acSearch(q) {
    this.acState.loading = true;
    try {
      const r = await fetch('/api/products/search?q=' + encodeURIComponent(q) + '&limit=6');
      if (!r.ok) { this.acState.results = []; return; }
      const d = await r.json();
      const list = Array.isArray(d) ? d : (d.products || d.results || []);
      this.acState.results = list;
      this.acState.active = 0;
      this.acState.expanded = null;
    } catch (_) {
      this.acState.results = [];
    } finally {
      this.acState.loading = false;
    }
  },
  acMove(dir) {
    if (!this.acState.results.length) return;
    const n = this.acState.results.length;
    this.acState.active = (this.acState.active + dir + n) % n;
  },
  acToggleExpand(productId) {
    if (!productId) return;
    if (this.acState.expanded === productId) {
      this.acState.expanded = null;
      return;
    }
    this.acState.expanded = productId;
    if (this.acState.detailCache[productId] || this.acState.detailLoading[productId]) return;
    this._acFetchDetail(productId);
  },
  acToggleExpandActive() {
    const p = this.acState.results[this.acState.active];
    if (p && p.product_id) this.acToggleExpand(p.product_id);
  },
  async _acFetchDetail(productId) {
    this.acState.detailLoading[productId] = true;
    this.acState.detailError[productId] = null;
    try {
      const r = await fetch('/api/products/' + encodeURIComponent(productId));
      if (!r.ok) {
        this.acState.detailError[productId] = r.status === 404 ? 'Product not found' : 'Failed to load details';
        return;
      }
      const detail = await r.json();
      // Fall back to the search-row data for fields the detail endpoint
      // might omit, so the panel never regresses what the row already shows.
      const row = this.acState.results.find(x => x && x.product_id === productId) || {};
      this.acState.detailCache[productId] = Object.assign({}, row, detail);
    } catch (_) {
      this.acState.detailError[productId] = 'Network error';
    } finally {
      this.acState.detailLoading[productId] = false;
    }
  },
  acSelectActive() {
    const p = this.acState.results[this.acState.active];
    if (!p) return;
    const idx = this.acState.idx;
    this.acClose();
    this.linkProduct(idx, p);
  },
  acUseManual(text) {
    const idx = this.acState.idx;
    this.acClose();
    if (idx === null || idx === undefined) return;
    const row = this.ings[idx]; if (!row) return;
    row.name = (text || row.name).trim();
    row.product_id = null;
    row.override = true;
    row.override_reason = row.override_reason || 'Manual entry';
    this._putIngredientsNow(this._serialize(this.ings),
      { flashIdx: idx, label: 'manual entry' });
  },

  // ── Smart "your usuals" (account-scoped) ─────────────────────────
  // Fetch suggestions in parallel with live search. Guarded by idx so a
  // late response from a previously-focused row never lands in the popover
  // now open over a different ingredient.
  async _acFetchSuggestions(idx, name) {
    if (!window._ssSmartLink) { this.acSuggest.loading = false; return; }
    try {
      const d = await window._ssSmartLink.fetchSuggestions(name, 6);
      if (this.acState.idx !== idx) return;
      this.acSuggest.items = d.suggestions || [];
      this.acSuggest.best = d.best_guess || null;
      this.acSuggest.canonical = d.canonical_name || null;
      this.acSuggest.canonicalConf = d.canonical_confidence ?? null;
    } catch (_) {
      this.acSuggest.items = [];
    } finally {
      this.acSuggest.loading = false;
    }
  },
  acIsBestGuess(p) {
    return !!(this.acSuggest.best && p && p.product_id === this.acSuggest.best.product_id);
  },
  acSelectSuggestion(p) {
    if (!p) return;
    const idx = this.acState.idx;
    this.acClose();
    // Suggestions carry product_id + product_description; linkProduct wants
    // a product-shaped object (description / product_id / category).
    this.linkProduct(idx, {
      product_id: p.product_id,
      description: p.product_description,
      category: null,
    });
  },
  acAcceptCanonical() {
    const idx = this.acState.idx;
    const canon = this.acSuggest.canonical;
    if (idx === null || idx === undefined || !canon) return;
    const row = this.ings[idx]; if (!row) return;
    row.name = canon;
    this.acSuggest.canonical = null;  // dismiss the chip
    this._putIngredientsNow(this._serialize(this.ings),
      { flashIdx: idx, label: 'standardized name' });
  },
});
