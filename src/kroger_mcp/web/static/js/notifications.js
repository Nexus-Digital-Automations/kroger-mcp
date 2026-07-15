/**
 * Notification bell — favorite-on-sale alerts.
 *
 * Renders globally from base.html on authenticated pages. Polls
 * /api/notifications, shows a dropdown popup of favorites that just went on
 * sale, and lets the user act on each inline (add to list / cart / view).
 *
 * Uses window.api (api-client.js) for action POSTs (auto error-toast) and a
 * plain fetch for the silent background poll (no toast on transient failure).
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('notifBell', () => ({
    open: false,
    alerts: [],
    pendingMeals: [],
    unseen: 0,
    busy: {},

    init() {
      this.refresh();
      // First polling loop in the app; 60s is ample for once-a-day sale scans.
      setInterval(() => this.refresh(), 60000);
    },

    async refresh() {
      // Plain fetch + silent failure: a background poll must not spam toasts.
      try {
        const res = await fetch('/api/notifications', {
          headers: { Accept: 'application/json' },
        });
        if (!res.ok) return;
        const d = await res.json();
        this.alerts = d.alerts || [];
        this.pendingMeals = d.pending_meals || [];
        this.unseen = d.unseen || 0;
      } catch (e) {
        /* offline / transient — keep prior state */
      }
    },

    async toggle() {
      this.open = !this.open;
      // Resync on open so meals confirmed/skipped elsewhere (or on a prior
      // page, before a full reload) aren't shown stale.
      if (this.open) await this.refresh();
      if (this.open && this.unseen > 0) {
        this.unseen = 0; // optimistic; server clears the badge state
        try {
          await window.api.post('/api/notifications/mark-seen');
        } catch (e) {
          /* error already surfaced by api-client */
        }
      }
    },

    priceLabel(a) {
      const sale = a.sale_price != null ? '$' + Number(a.sale_price).toFixed(2) : '';
      const reg = a.regular_price != null ? '$' + Number(a.regular_price).toFixed(2) : '';
      const pct = a.savings_percent ? ' · −' + Math.round(a.savings_percent) + '%' : '';
      return reg && sale ? `${sale} (was ${reg})${pct}` : sale + pct;
    },

    async _remove(a, acted) {
      try {
        await window.api.post('/api/notifications/' + a.id + '/dismiss', {
          acted: !!acted,
        });
      } catch (e) {
        return;
      }
      this.alerts = this.alerts.filter((x) => x.id !== a.id);
    },

    async dismiss(a) {
      await this._remove(a, false);
    },

    async addToList(a) {
      if (this.busy[a.id]) return;
      this.busy = { ...this.busy, [a.id]: true };
      try {
        await window.api.post('/api/shopping-list/items', {
          product_id: a.product_id,
          name: a.description,
          quantity: a.default_quantity || 1,
          unit: '',
        });
        window._ssToast('Added to list');
        await this._remove(a, true);
      } catch (e) {
        /* api-client toasted the error */
      } finally {
        const b = { ...this.busy };
        delete b[a.id];
        this.busy = b;
      }
    },

    async addToCart(a) {
      if (this.busy[a.id]) return;
      const qty = await Alpine.store('qtyPicker').ask(a);
      if (qty == null) return;
      this.busy = { ...this.busy, [a.id]: true };
      try {
        await window.api.post('/api/products/' + a.product_id + '/add-to-cart', {
          quantity: qty,
          modality: a.preferred_modality || 'PICKUP',
          description: a.description,
          price: a.sale_price || 0,
        });
        window._ssToast('Added to cart');
        await this._remove(a, true);
      } catch (e) {
        /* api-client toasted the error */
      } finally {
        const b = { ...this.busy };
        delete b[a.id];
        this.busy = b;
      }
    },

    view(a) {
      window.location.href = '/favorites/' + (a.list_id || '');
    },

    confirmMeal(m) {
      // Reuses the existing cook-confirmation modal (global store, promoted
      // to base.html) — it owns the actual /cooked POST and pantry deduction.
      Alpine.store('cookPreview').openModal('meal', {
        planId: m.plan_id,
        date: m.meal_date,
        slot: m.meal_slot,
      });
    },

    async skipMeal(m) {
      if (this.busy[m.id]) return;
      this.busy = { ...this.busy, [m.id]: true };
      try {
        await window.api.post(
          '/api/meal-plan/' + m.plan_id + '/meals/' + m.meal_date + '/' + m.meal_slot + '/skip'
        );
        window._ssToast("Marked as not cooked");
        this.pendingMeals = this.pendingMeals.filter((x) => x.id !== m.id);
      } catch (e) {
        /* api-client toasted the error */
      } finally {
        const b = { ...this.busy };
        delete b[m.id];
        this.busy = b;
      }
    },

    async confirmAllMeals() {
      if (this.busy.__confirmAll) return;
      this.busy = { ...this.busy, __confirmAll: true };
      try {
        const res = await window.api.post('/api/meal-plan/pending/confirm-all');
        window._ssToast('Confirmed ' + (res.reconciled || 0) + ' meal(s) as cooked');
        this.pendingMeals = [];
      } catch (e) {
        /* api-client toasted the error */
      } finally {
        const b = { ...this.busy };
        delete b.__confirmAll;
        this.busy = b;
      }
    },
  }));
});
