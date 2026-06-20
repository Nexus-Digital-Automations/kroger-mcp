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
        this.unseen = d.unseen || 0;
      } catch (e) {
        /* offline / transient — keep prior state */
      }
    },

    async toggle() {
      this.open = !this.open;
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
      this.busy = { ...this.busy, [a.id]: true };
      try {
        await window.api.post('/api/products/' + a.product_id + '/add-to-cart', {
          quantity: a.default_quantity || 1,
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
  }));
});
