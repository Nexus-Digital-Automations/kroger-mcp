/**
 * Dashboard "Needs your attention" feed — one ranked list instead of six
 * equal-weight cards.
 *
 * Fetches the same /api/notifications payload the bell already polls
 * (favorite-on-sale alerts, meals awaiting cook-confirmation, pantry
 * alerts, next-week-plan reminder) so the two surfaces never disagree on
 * what counts as an alert, then groups everything into three urgency
 * tiers. Overdue favorites and smart suggestions are dashboard-only
 * signals not part of the bell payload, so they're seeded server-side
 * (Jinja `tojson`) as init args instead.
 *
 * Action methods mirror notifBell (notifications.js) — same endpoints,
 * same window.api / window._ssToast / Alpine store usage — so acting on a
 * row here behaves identically to acting on it from the bell.
 *
 * overdueFavorites/smartSuggestions are seeded server-side into a
 * <script type="application/json" id="priority-feed-seed"> tag (see
 * dashboard.html) rather than passed as Alpine.data() args, since Jinja2's
 * `tojson` filter output isn't safe to inline into an HTML attribute
 * (unescaped double-quotes would close the x-data="..." attribute early).
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('priorityFeed', () => {
    const seedEl = document.getElementById('priority-feed-seed');
    const seed = seedEl ? JSON.parse(seedEl.textContent) : {};
    return {
    loading: true,
    alerts: [],
    pendingMeals: [],
    pantryAlerts: [],
    needsPlan: false,
    overdueFavorites: seed.overdue_favorites || [],
    smartSuggestions: seed.smart_suggestions || [],
    busy: {},

    init() {
      this.refresh();
    },

    async refresh() {
      try {
        const res = await fetch('/api/notifications', {
          headers: { Accept: 'application/json' },
        });
        if (!res.ok) return;
        const d = await res.json();
        this.alerts = d.alerts || [];
        this.pendingMeals = d.pending_meals || [];
        this.pantryAlerts = d.pantry_alerts || [];
        this.needsPlan = !!d.needs_plan;
      } catch (e) {
        /* offline / transient — keep prior state */
      } finally {
        this.loading = false;
      }
    },

    // -- Tier derivation ---------------------------------------------------
    // A meal more than 2 days uncooked has gone from "confirm when you get
    // a chance" to "this is stale" — matches the 3-day cutoff a human would
    // draw between "yesterday's dinner" and "over the weekend forgotten".
    get critical() {
      const pantryOut = this.pantryAlerts
        .filter((p) => p.status === 'out')
        .map((p) => ({ kind: 'pantry', data: p }));
      const staleMeals = this.pendingMeals
        .filter((m) => this._daysOverdue(m) >= 3)
        .map((m) => ({ kind: 'meal', data: m }));
      const overdueFavs = this.overdueFavorites.map((f) => ({ kind: 'favorite', data: f }));
      return [...pantryOut, ...staleMeals, ...overdueFavs];
    },

    get thisWeek() {
      const pantryLow = this.pantryAlerts
        .filter((p) => p.status !== 'out')
        .map((p) => ({ kind: 'pantry', data: p }));
      const sales = this.alerts.map((a) => ({ kind: 'sale', data: a }));
      const recentMeals = this.pendingMeals
        .filter((m) => this._daysOverdue(m) < 3)
        .map((m) => ({ kind: 'meal', data: m }));
      const overdueSuggestions = this.smartSuggestions
        .filter((s) => s.tag_kind === 'danger')
        .map((s) => ({ kind: 'suggestion', data: s }));
      return [...pantryLow, ...sales, ...recentMeals, ...overdueSuggestions];
    },

    get planAhead() {
      const plan = this.needsPlan ? [{ kind: 'plan', data: {} }] : [];
      const seasonal = this.smartSuggestions
        .filter((s) => s.tag_kind !== 'danger')
        .map((s) => ({ kind: 'suggestion', data: s }));
      return [...plan, ...seasonal];
    },

    get totalCount() {
      return this.critical.length + this.thisWeek.length + this.planAhead.length;
    },

    _daysOverdue(m) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const mealDate = new Date(m.meal_date + 'T00:00:00');
      return Math.round((today - mealDate) / 86400000);
    },

    priceLabel(a) {
      const sale = a.sale_price != null ? '$' + Number(a.sale_price).toFixed(2) : '';
      const reg = a.regular_price != null ? '$' + Number(a.regular_price).toFixed(2) : '';
      const pct = a.savings_percent ? ' · −' + Math.round(a.savings_percent) + '%' : '';
      return reg && sale ? `${sale} (was ${reg})${pct}` : sale + pct;
    },

    // -- Actions (same endpoints/behavior as the bell's notifBell) --------
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
        await window.api.post('/api/notifications/' + a.id + '/dismiss', { acted: true });
        this.alerts = this.alerts.filter((x) => x.id !== a.id);
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
        await window.api.post('/api/notifications/' + a.id + '/dismiss', { acted: true });
        this.alerts = this.alerts.filter((x) => x.id !== a.id);
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

    async reorderFavorite(f) {
      if (this.busy[f.list_id]) return;
      this.busy = { ...this.busy, [f.list_id]: true };
      try {
        const res = await window.api.post(
          '/api/favorites/lists/' + f.list_id + '/add-to-shopping-list'
        );
        window._ssToast('Added ' + (res.items_added || 0) + ' item(s) to your list');
        this.overdueFavorites = this.overdueFavorites.filter((x) => x.list_id !== f.list_id);
      } catch (e) {
        /* api-client toasted the error */
      } finally {
        const b = { ...this.busy };
        delete b[f.list_id];
        this.busy = b;
      }
    },
    };
  });
});
