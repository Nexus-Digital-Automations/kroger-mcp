/**
 * ingredientPanel — the recipe Ingredients card Alpine component.
 *
 * Ownership: the single source for the ingredient table, servings stepper,
 * inline edit/save, and the Kroger product-linking popover (see
 * templates/_macros/ingredient_linker_popover.html for the markup it drives).
 * Shared verbatim by recipe_view.html and recipe_edit.html — it was duplicated
 * inline in both (byte-identical, diff-verified) and extracted here so a fix
 * lands once. Server data arrives via the #recipe-init-data JSON script tag
 * ({id, name, servings, ingredients, ...}); no Jinja in this file.
 */
document.addEventListener('alpine:init', () => {
  const _initData = (() => {
    try {
      return JSON.parse(document.getElementById('recipe-init-data').textContent) || {};
    } catch (e) { return {}; }
  })();

  // Bridge: the header "Add to List" button lives outside the ingredients
  // panel that owns the servings stepper. This store carries the live
  // servings count across so the preview modal opens with the user's
  // current scaling. ingredientPanel.init() writes on every change.
  Alpine.store('recipeServings', { value: 0 });

    Alpine.data('ingredientPanel', () => ({
      ingView: 'usage',
      ings: _initData.ingredients || [],
      baseServings: _initData.servings || 4,
      servings: _initData.servings || 4,

      init() {
        // Mirror servings into the shared store so the header button
        // opens the preview at the value the user currently sees on the stepper.
        this.$watch('servings', v => Alpine.store('recipeServings').value = v);
        Alpine.store('recipeServings').value = this.servings;
      },
      gradeColor(g) {
        const m = {
          A: {bg:'oklch(91% 0.1 145)',  text:'oklch(30% 0.14 145)'},
          B: {bg:'oklch(91% 0.08 175)', text:'oklch(30% 0.12 175)'},
          C: {bg:'oklch(92% 0.1 80)',   text:'oklch(35% 0.12 75)'},
          D: {bg:'oklch(92% 0.1 50)',   text:'oklch(33% 0.14 40)'},
          F: {bg:'oklch(92% 0.08 25)',  text:'oklch(33% 0.14 25)'},
        };
        return m[g] || m.C;
      },
      catLabels: { produce: 'Produce', meat: 'Meat', seafood: 'Seafood', dairy: 'Dairy', frozen: 'Frozen', pantry: 'Pantry', beverages: 'Beverages', bakery: 'Bakery', other: 'Other' },
      catOrder: ['produce', 'meat', 'seafood', 'dairy', 'frozen', 'pantry', 'beverages', 'bakery', 'other'],
      catNorm: { vegetables: 'produce', aromatics: 'produce', greens: 'produce', herbs: 'produce', protein: 'meat', poultry: 'meat', baking: 'pantry', 'canned goods': 'pantry', condiments: 'pantry', grains: 'pantry', oil: 'pantry', oils: 'pantry', sauce: 'pantry', sauces: 'pantry', spices: 'pantry', acid: 'pantry', seasoning: 'pantry' },
      catColors: {
        produce:   { pill: 'oklch(91% 0.1 145)',  text: 'oklch(28% 0.14 145)', dot: 'oklch(50% 0.19 145)' },
        meat:      { pill: 'oklch(92% 0.08 22)',   text: 'oklch(30% 0.14 22)',  dot: 'oklch(50% 0.18 22)'  },
        seafood:   { pill: 'oklch(91% 0.08 220)',  text: 'oklch(30% 0.13 220)', dot: 'oklch(50% 0.16 220)' },
        dairy:     { pill: 'oklch(92% 0.06 240)',  text: 'oklch(32% 0.11 240)', dot: 'oklch(54% 0.14 240)' },
        frozen:    { pill: 'oklch(91% 0.09 268)',  text: 'oklch(30% 0.13 268)', dot: 'oklch(50% 0.16 268)' },
        pantry:    { pill: 'oklch(92% 0.1 72)',    text: 'oklch(30% 0.16 68)',  dot: 'oklch(56% 0.19 70)'  },
        beverages: { pill: 'oklch(91% 0.08 300)',  text: 'oklch(30% 0.11 300)', dot: 'oklch(50% 0.14 300)' },
        bakery:    { pill: 'oklch(92% 0.09 52)',   text: 'oklch(30% 0.14 48)',  dot: 'oklch(56% 0.18 52)'  },
        other:     { pill: 'oklch(92% 0.018 80)',  text: 'oklch(38% 0.03 80)',  dot: 'oklch(55% 0.05 80)'  },
      },

      inc() { if (this.servings < 50) this.servings++; },
      dec() { if (this.servings > 1)  this.servings--; },

      // Display a scaled quantity. Returns '' only when there is genuinely
      // nothing to show (null/NaN). Never silently emits an empty string
      // for a *positive* result \u2014 that used to mask scaling bugs by falling
      // back to the unscaled value in the template.
      fmtQty(raw) {
        if (raw === null || raw === undefined) return '';
        const n = parseFloat(raw);
        if (isNaN(n)) return '';
        if (n === 0) return '0';
        // Snap to integer only when the rounded value is non-zero — otherwise
        // 0.001 would silently become '0', resurrecting the silent-zero bug.
        const _rnd = Math.round(n);
        if (_rnd !== 0 && Math.abs(n - _rnd) < 0.005) return String(_rnd);
        const sign  = n < 0 ? '-' : '';
        const abs   = Math.abs(n);
        const whole = Math.floor(abs);
        const frac  = abs - whole;
        const table = [
          [0.0625, '\u2009\u00b9\u2044\u2081\u2086'], // 1/16 \u2014 fall back to literal when no precomposed glyph exists
          [0.125, '\u215b'],   // \u215b
          [0.1667, '\u2009\u00b9\u2044\u2086'], // 1/6
          [0.25, '\u00bc'],    // \u00bc
          [0.333, '\u2153'],   // \u2153
          [0.375, '\u215c'],   // \u215c
          [0.5, '\u00bd'],     // \u00bd
          [0.625, '\u215d'],   // \u215d
          [0.667, '\u2154'],   // \u2154
          [0.75, '\u00be'],    // \u00be
          [0.833, '\u2009\u2075\u2044\u2086'], // 5/6
          [0.875, '\u215e'],   // \u215e
        ];
        for (const [v, sym] of table) {
          if (Math.abs(frac - v) < 0.02) return sign + (whole ? whole + '\u2009' : '') + sym;
        }
        // Decimal fallback \u2014 never round a positive value to '0'.
        if (abs < 0.05) return sign + abs.toFixed(2);
        const r = Math.round(abs * 10) / 10;
        if (r === Math.floor(r)) return sign + String(Math.floor(r));
        return sign + r.toFixed(1);
      },

      get scaledIngs() {
        const f = this.servings / this.baseServings;
        return this.ings.map((i, idx) => ({
          ...i,
          _idx: idx,
          sq: (i.quantity !== null && i.quantity !== undefined) ? i.quantity * f : null
        }));
      },

      get groups() {
        // View mode reads as a single flowing list; category grouping is an
        // edit-mode aid for organizing ingredients while authoring.
        const editing = Alpine.store('recipeMode')?.editing;
        if (!editing || this.ingView === 'usage') return [{ header: null, key: null, items: this.scaledIngs }];
        const map = {};
        this.scaledIngs.forEach(i => {
          const raw = ((i.category || '') + '').toLowerCase().trim();
          const key = this.catOrder.includes(this.catNorm[raw] || raw) ? (this.catNorm[raw] || raw) : 'other';
          (map[key] = map[key] || []).push(i);
        });
        return this.catOrder.filter(k => map[k]).map(k => ({ header: this.catLabels[k], key: k, items: map[k] }));
      },

      // ── Inline ingredient editing ─────────────────────────────────────
      // All mutations go through PUT /api/recipes/{id}/ingredients atomically.
      // After success we silently refetch the enriched ingredient list so
      // safety/pantry chips reflect the server's recomputed view without
      // a full HTML reload. Concurrent rapid edits are debounced (600ms)
      // and a monotonic _saveSeq drops stale responses from racing
      // earlier in-flight saves.
      _recipeId() {
        const el = document.getElementById('recipe-init-data');
        return el ? JSON.parse(el.textContent).id : null;
      },
      _saveSeq: 0,
      _debounceTimer: null,
      _pendingNext: null,
      _flashRow(idx) {
        const row = document.querySelector('[data-ingredient-idx="' + idx + '"]');
        if (!row) return;
        row.classList.remove('ss-saved-pulse');
        void row.offsetWidth;
        row.classList.add('ss-saved-pulse');
      },
      async _putIngredientsNow(next, opts) {
        const seq = ++this._saveSeq;
        const flashIdx = opts && opts.flashIdx;
        const label = (opts && opts.label) || 'ingredient';
        try {
          const payload = await window._ssRecipeSave(
            'PUT',
            '/api/recipes/' + this._recipeId() + '/ingredients',
            { ingredients: next }
          );
          if (seq !== this._saveSeq) return false;
          await this._refreshEnriched();
          if (flashIdx !== undefined) this._flashRow(flashIdx);
          window._ssToast('Saved ' + label);
          return true;
        } catch (e) {
          if (seq === this._saveSeq) {
            this.ings = (typeof _initIngs === 'function') ? _initIngs() : this.ings;
          }
          return false;
        }
      },
      _putIngredientsDebounced(next, opts) {
        this._pendingNext = next;
        const flashIdx = opts && opts.flashIdx;
        const label = opts && opts.label;
        if (this._debounceTimer) clearTimeout(this._debounceTimer);
        this._debounceTimer = setTimeout(() => {
          const toSend = this._pendingNext;
          this._pendingNext = null;
          this._debounceTimer = null;
          this._putIngredientsNow(toSend, { flashIdx, label });
        }, 600);
      },
      async _refreshEnriched() {
        try {
          const r = await fetch('/api/recipes/' + this._recipeId() + '/ingredients');
          if (!r.ok) return;
          const d = await r.json();
          if (d && d.success && Array.isArray(d.ingredients)) {
            this.ings = d.ingredients;
          }
        } catch (_) { /* refresh is best-effort; the next save will retry */ }
      },
      _serialize(ings) {
        return ings.map(i => ({
          name: i.name,
          quantity: i.quantity,
          unit: i.unit,
          category: i.category,
          product_id: i.product_id,
          override: !!i.override,
          override_reason: i.override_reason,
        }));
      },
      saveIngredientField(idx, field, raw, $el) {
        const current = this.ings[idx];
        if (!current) return;
        let value = raw;
        if (field === 'quantity') {
          const trimmed = String(raw).trim();
          if (trimmed === '') { value = null; }
          else {
            const n = parseFloat(trimmed);
            if (isNaN(n) || n < 0) { if ($el) $el.innerText = String(current.quantity ?? ''); return; }
            value = n;
          }
        } else {
          value = (raw ?? '').trim() || null;
        }
        if (field === 'name' && !value) {
          if ($el) $el.innerText = current.name;
          return;
        }
        if (value === current[field]) return;
        this.ings[idx][field] = value;  // optimistic
        const next = this._serialize(this.ings);
        this._putIngredientsDebounced(next, { flashIdx: idx, label: field });
      },
      linkProduct(idx, product) {
        // Wires a Kroger product into an ingredient row, flipping it out of
        // override and refreshing safety/pantry. Used by the autocomplete
        // popover and the "Manual" pill picker.
        if (!product) return;
        const row = this.ings[idx]; if (!row) return;
        row.name = product.description || row.name;
        row.product_id = product.product_id;
        row.override = false;
        row.override_reason = null;
        if (!row.category && product.category) row.category = product.category;
        this._putIngredientsNow(this._serialize(this.ings),
          { flashIdx: idx, label: 'Kroger link' });
      },
      unlinkProduct(idx) {
        const row = this.ings[idx]; if (!row) return;
        row.product_id = null;
        row.override = true;
        row.override_reason = 'Unlinked from Kroger product';
        this._putIngredientsNow(this._serialize(this.ings),
          { flashIdx: idx, label: 'unlink' });
      },
      removeIngredient(idx) {
        const target = this.ings[idx];
        if (!target) return;
        // Optimistic remove + 5s undo via toast action. The "snapshot"
        // captures the entire current list so an Undo restores the row at
        // the same position even after subsequent edits.
        const snapshot = this._serialize(this.ings);
        const next = snapshot.filter((_, i) => i !== idx);
        const targetName = target.name || 'ingredient';
        this.ings.splice(idx, 1);
        this._putIngredientsNow(next, { label: 'removal' }).then(ok => {
          if (!ok) return;
          window._ssToast('Removed “' + targetName + '”', {
            action: {
              label: 'Undo',
              onClick: () => this._putIngredientsNow(snapshot, { label: 'restore' }),
            },
          });
        });
      },
      reorderIngredient(fromIdx, toIdx) {
        if (fromIdx === toIdx) return;
        if (fromIdx < 0 || toIdx < 0) return;
        if (fromIdx >= this.ings.length || toIdx > this.ings.length) return;
        const next = this._serialize(this.ings);
        const [moved] = next.splice(fromIdx, 1);
        next.splice(toIdx, 0, moved);
        this.ings = next;
        this._putIngredientsNow(next, { label: 'reorder' });
      },
      async addIngredient(rawQty, rawUnit, rawName) {
        const name = (rawName || '').trim();
        if (!name) return;
        let qty = null;
        const tq = String(rawQty || '').trim();
        if (tq) {
          const n = parseFloat(tq);
          if (!isNaN(n) && n >= 0) qty = n;
        }
        const unit = (rawUnit || '').trim() || null;
        const next = this._serialize(this.ings).concat([{
          name, quantity: qty, unit, category: null,
          product_id: null, override: true,
          override_reason: 'Added inline; not yet linked to Kroger product'
        }]);
        this.ings = next;  // optimistic
        return this._putIngredientsNow(next, { label: 'new ingredient' });
      },

      // ── Product-linking popover (search, usuals, detail, anchoring) ──
      // Lives in linker_popover.js as a mixin; spread keeps `this` shared.
      ...window._ssLinkerPopoverMixin(),

    }));
});
