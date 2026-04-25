/**
 * Unified action-menu Alpine component for product, recipe, favorites-detail
 * and shopping-list cards.
 *
 * OWNS: interaction state (open/close, cascading submenu activation, hover-
 * intent close timing, keyboard traversal, focus return, mobile-vs-desktop
 * mode detection) and the shared target-entity store populated from the
 * server-rendered #action-menu-data JSON bundle.
 *
 * DOES NOT OWN: actual network calls. Each leaf dispatches a CustomEvent on
 * the menu root (e.g. 'action-menu:favorites-add') and the host page's
 * existing Alpine component handles the fetch. This preserves the existing
 * per-page network code and its Playwright test coverage.
 *
 * LOADED BY: templates/base.html via <script defer src="/static/js/action_menu.js">.
 * READS: <script id="action-menu-data" type="application/json"> page-level
 * bundle emitted by base.html from action_menu_context().
 *
 * State machine (per-instance):
 *
 *     closed ──toggle()──▶ open(level=1)
 *     open(level=1, activeSub=null) ──openSub(name)──▶ open(level=2, activeSub=name)
 *     open(level=2) ──back()|Escape|ArrowLeft──▶ open(level=1)
 *     open(*) ──close()|click-away|activate-leaf──▶ closed (focus→trigger)
 *
 * Mode is derived from matchMedia('(hover: hover) and (min-width: 768px)') at
 * init and on resize. Desktop: level 2 is a flyout next to level 1. Mobile:
 * level 2 replaces level 1 with a back button — same state, different layout.
 *
 * @stable — the CustomEvent names below are the contract host Alpine
 * components listen to. Renaming requires updating all template listeners.
 *   action-menu:favorites-add        detail: {item, listId, listName}
 *   action-menu:favorites-new-list   detail: {item}
 *   action-menu:favorites-move       detail: {item, listId, listName}
 *   action-menu:cart-add             detail: {item}                       (Kroger cart)
 *   action-menu:shopping-add         detail: {item}                       (local shopping list)
 *   action-menu:shopping-remove      detail: {item}
 *   action-menu:recipe-add           detail: {item, recipeId, recipeName}
 *   action-menu:view-details         detail: {item}
 *   action-menu:meal-plan-add        detail: {item}
 *   action-menu:edit                 detail: {item}
 *   action-menu:delete               detail: {item}
 */

document.addEventListener('alpine:init', () => {
  const dataEl = document.getElementById('action-menu-data');
  let bundle = { favoritesLists: [], recipes: [], mealPlans: [] };
  if (dataEl) {
    try {
      bundle = JSON.parse(dataEl.textContent || '{}');
    } catch (err) {
      console.error('[actionMenu] failed to parse #action-menu-data', err);
    }
  }

  window.Alpine.store('actionMenu', {
    favoritesLists: bundle.favoritesLists || [],
    recipes: bundle.recipes || [],
    mealPlans: bundle.mealPlans || [],

    // Called by host pages after creating a new favorites list so every open
    // menu reflects the change without a full reload.
    async refreshFavorites() {
      try {
        const res = await fetch('/api/favorites/lists');
        if (!res.ok) return;
        this.favoritesLists = await res.json();
      } catch (err) {
        console.error('[actionMenu] refreshFavorites failed', err);
      }
    },
  });

  window.Alpine.data('actionMenu', (config) => ({
    config: config || {},
    open: false,
    activeSub: null,
    mode: 'desktop',
    _hoverTimer: null,
    _resizeHandler: null,

    init() {
      this._setMode();
      this._resizeHandler = () => this._setMode();
      window.addEventListener('resize', this._resizeHandler);
    },

    destroy() {
      if (this._resizeHandler) window.removeEventListener('resize', this._resizeHandler);
      this._clearHoverTimer();
    },

    _setMode() {
      // Hover-capable pointer AND wide viewport = desktop cascade.
      // Otherwise accordion drill-down (replaces content + back button).
      const mql = window.matchMedia('(hover: hover) and (min-width: 768px)');
      this.mode = mql.matches ? 'desktop' : 'mobile';
    },

    toggle() {
      if (this.open) this.close();
      else this.openMenu();
    },

    openMenu() {
      this.open = true;
      this.activeSub = null;
      this.$nextTick(() => this._focusFirstAt('[data-menu-level="root"]'));
    },

    close() {
      if (!this.open) return;
      this.open = false;
      this.activeSub = null;
      this._clearHoverTimer();
      this.$nextTick(() => {
        if (this.$refs.trigger) this.$refs.trigger.focus();
      });
    },

    clickAway(event) {
      // Click-away must NOT fire for clicks inside any nested submenu, even
      // when the submenu renders inside a z-index stacking context that
      // looks visually "outside" the root.
      const root = this.$el;
      if (event && event.target && root.contains(event.target)) return;
      this.close();
    },

    openSub(name) {
      this._clearHoverTimer();
      this.activeSub = name;
      this.$nextTick(() => this._focusFirstAt(`[data-menu-level="${name}"]`));
    },

    // Schedule close gives a generous 180ms hover-intent window so diagonal
    // cursor paths between the level-1 trigger and the level-2 flyout don't
    // prematurely dismiss. Entering the flyout cancels the timer.
    scheduleClose(name) {
      this._clearHoverTimer();
      this._hoverTimer = setTimeout(() => {
        if (this.activeSub === name) this.activeSub = null;
      }, 180);
    },

    clearCloseTimer() {
      this._clearHoverTimer();
    },

    _clearHoverTimer() {
      if (this._hoverTimer) {
        clearTimeout(this._hoverTimer);
        this._hoverTimer = null;
      }
    },

    back() {
      if (this.activeSub !== null) {
        const subName = this.activeSub;
        this.activeSub = null;
        this.$nextTick(() => {
          const trigger = this.$el.querySelector(`[data-submenu-trigger="${subName}"]`);
          if (trigger) trigger.focus();
          else this._focusFirstAt('[data-menu-level="root"]');
        });
      } else {
        this.close();
      }
    },

    act(eventName, payload) {
      // Leaves dispatch bubble:true so the host page's Alpine component can
      // listen at a scope higher than the card without knowing each card's
      // DOM shape.
      this.$el.dispatchEvent(
        new CustomEvent(`action-menu:${eventName}`, {
          detail: { item: this.config.item, ...(payload || {}) },
          bubbles: true,
        })
      );
      this.close();
    },

    handleKey(event) {
      if (!this.open) return;
      switch (event.key) {
        case 'Escape':
          event.preventDefault();
          this.back();
          break;
        case 'ArrowDown':
          event.preventDefault();
          this._moveFocus(1);
          break;
        case 'ArrowUp':
          event.preventDefault();
          this._moveFocus(-1);
          break;
        case 'ArrowLeft':
          if (this.activeSub !== null) {
            event.preventDefault();
            this.back();
          }
          break;
        case 'ArrowRight':
          // Handled inline on submenu triggers via @keydown.arrow-right.
          break;
        case 'Home':
          event.preventDefault();
          this._moveFocusTo(0);
          break;
        case 'End':
          event.preventDefault();
          this._moveFocusTo(-1);
          break;
      }
    },

    _currentLevelSelector() {
      if (this.activeSub && (this.mode === 'mobile' || this.activeSub)) {
        return `[data-menu-level="${this.activeSub}"]`;
      }
      return '[data-menu-level="root"]';
    },

    _visibleMenuItems() {
      const level = this.$el.querySelector(this._currentLevelSelector());
      if (!level) return [];
      return Array.from(level.querySelectorAll('[role="menuitem"]')).filter(
        (el) => el.offsetParent !== null && !el.disabled
      );
    },

    _focusFirstAt(selector) {
      const level = this.$el.querySelector(selector);
      if (!level) return;
      const first = level.querySelector('[role="menuitem"]:not([disabled])');
      if (first) first.focus();
    },

    _moveFocus(delta) {
      const items = this._visibleMenuItems();
      if (items.length === 0) return;
      const activeEl = document.activeElement;
      const currentIdx = items.indexOf(activeEl);
      const nextIdx =
        currentIdx < 0
          ? delta > 0
            ? 0
            : items.length - 1
          : (currentIdx + delta + items.length) % items.length;
      items[nextIdx].focus();
    },

    _moveFocusTo(index) {
      const items = this._visibleMenuItems();
      if (items.length === 0) return;
      const target = index < 0 ? items[items.length - 1] : items[index];
      if (target) target.focus();
    },
  }));
});
