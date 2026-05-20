/**
 * sort_rank.js — Shared draggable multi-rank sort mixin for Alpine.js pages.
 *
 * Owner: web/templates (recipes, deals, products). Replaces the previous
 * per-template `sortStack` duplication. Single source of truth for the sort
 * dialog's state, persistence, and tiebreaker semantics.
 *
 * State shape: `sortStack: [{v: <option-key>, dir: 'asc'|'desc'}]`.
 * Higher index = lower tiebreaker priority. Persisted per-page in
 * localStorage under `opts.storageKey`.
 *
 * Spread the returned object into an Alpine factory, e.g.:
 *   Alpine.data('myGrid', () => ({
 *     ...window.sortRank({
 *       storageKey: 'sortRank:myPage',
 *       options:    [{v:'name', l:'Name'}, {v:'cost', l:'Cost', defaultDir:'asc'}],
 *       compareFns: { name: (a,b) => a.name.localeCompare(b.name),
 *                     cost: (a,b) => (a.cost||0) - (b.cost||0) },
 *       fallback:   [{v:'name', dir:'asc'}],
 *     }),
 *     init() { this._loadSortRank(); },
 *     get sorted() { return this.sortRunner(this.items); },
 *   }));
 *
 * Mixin exposes: sortStack, sortDialogOpen, sortOptions, sortLabel(v),
 * sortOptionsUnused(), addSort/removeSort/toggleDir/clearSort,
 * sortDragStart/Over/Drop/End, sortRunner(list), isNoDir(v).
 */
(function () {
  'use strict';

  const PERSIST_DELAY_MS = 300;

  function sortRank(opts) {
    if (!opts || typeof opts !== 'object') {
      throw new Error('sortRank: opts is required');
    }
    const storageKey = opts.storageKey;
    const options = Array.isArray(opts.options) ? opts.options : [];
    const compareFns = opts.compareFns || {};
    const fallback = Array.isArray(opts.fallback) ? opts.fallback : [];

    if (!storageKey) throw new Error('sortRank: opts.storageKey is required');
    if (!options.length) throw new Error('sortRank: opts.options is required');

    const optionByKey = new Map(options.map((o) => [o.v, o]));

    return {
      sortStack: [],
      sortDialogOpen: false,
      sortOptions: options,
      _sortDragKey: null,
      _sortDragOverKey: null,
      _sortSaveTimer: null,

      _loadSortRank() {
        try {
          const raw = localStorage.getItem(storageKey);
          if (!raw) return;
          const parsed = JSON.parse(raw);
          if (!Array.isArray(parsed)) return;
          this.sortStack = parsed
            .filter((i) => i && typeof i.v === 'string' && optionByKey.has(i.v))
            .map((i) => ({ v: i.v, dir: i.dir === 'desc' ? 'desc' : 'asc' }));
        } catch (e) {
          /* corrupt JSON → ignore, fall through to default */
        }
      },

      _persistSortRank() {
        clearTimeout(this._sortSaveTimer);
        const snapshot = this.sortStack.map((i) => ({ v: i.v, dir: i.dir }));
        this._sortSaveTimer = setTimeout(() => {
          try {
            localStorage.setItem(storageKey, JSON.stringify(snapshot));
          } catch (e) {
            /* quota or disabled → silent; in-memory state still works */
          }
        }, PERSIST_DELAY_MS);
      },

      sortLabel(v) {
        const o = optionByKey.get(v);
        return o ? o.l : v;
      },

      isNoDir(v) {
        const o = optionByKey.get(v);
        return !!(o && o.noDir);
      },

      sortOptionsUnused() {
        const active = new Set(this.sortStack.map((i) => i.v));
        return options.filter((o) => !active.has(o.v));
      },

      addSort(v) {
        if (!optionByKey.has(v)) return;
        if (this.sortStack.some((i) => i.v === v)) return;
        const opt = optionByKey.get(v);
        const dir = opt.defaultDir === 'desc' ? 'desc' : 'asc';
        this.sortStack = [...this.sortStack, { v, dir }];
        this._persistSortRank();
      },

      removeSort(v) {
        this.sortStack = this.sortStack.filter((i) => i.v !== v);
        this._persistSortRank();
      },

      toggleDir(v) {
        if (this.isNoDir(v)) return;
        this.sortStack = this.sortStack.map((i) =>
          i.v === v ? { v: i.v, dir: i.dir === 'asc' ? 'desc' : 'asc' } : i
        );
        this._persistSortRank();
      },

      clearSort() {
        this.sortStack = [];
        this._persistSortRank();
      },

      sortDragStart(v) {
        this._sortDragKey = v;
      },

      sortDragOver(v) {
        if (v !== this._sortDragKey) this._sortDragOverKey = v;
      },

      sortDrop() {
        const from = this._sortDragKey;
        const to = this._sortDragOverKey;
        this.sortDragEnd();
        if (!from || !to || from === to) return;
        const fi = this.sortStack.findIndex((i) => i.v === from);
        const ti = this.sortStack.findIndex((i) => i.v === to);
        if (fi < 0 || ti < 0) return;
        const next = [...this.sortStack];
        const [moved] = next.splice(fi, 1);
        next.splice(ti, 0, moved);
        this.sortStack = next;
        this._persistSortRank();
      },

      sortDragEnd() {
        this._sortDragKey = null;
        this._sortDragOverKey = null;
      },

      sortRunner(list) {
        const stack = this.sortStack.length > 0 ? this.sortStack : fallback;
        if (stack.length === 0) return [...list];
        const ctx = this;
        return [...list].sort((a, b) => {
          for (const { v, dir } of stack) {
            const fn = compareFns[v];
            if (!fn) continue;
            let r = fn(a, b, ctx);
            if (dir === 'desc') r = -r;
            if (r !== 0) return r;
          }
          return 0;
        });
      },
    };
  }

  window.sortRank = sortRank;
})();
