/**
 * Shared API client for the Smart Shopper web UI.
 *
 * Owns:
 *   - fetch()-wrapper that auto-JSON-encodes request bodies, attaches Content-Type,
 *     dispatches a 'toast:show' custom event on errors, and deduplicates concurrent
 *     identical requests so rapid double-clicks don't fire duplicate POSTs.
 *
 * Does NOT own:
 *   - Auth token management (session cookie set by the server).
 *   - Toast rendering — hosts listen for 'toast:show' events. See any page template
 *     that defines an Alpine data object with a `toast` property for the rendering side.
 *   - Server-side deduplication — this is client-side best-effort only.
 *
 * Called by: every page template's Alpine x-data factory (products, shopping_list,
 * recipes, favorites, pantry, meal_planner, chat widget, etc.).
 *
 * Calls: fetch(), window.dispatchEvent(CustomEvent).
 *
 * Deduplication is keyed by method + path + JSON.stringify(body). Only
 * truly identical concurrent requests collapse; different bodies for the
 * same path are independent operations. The dedupe TTL is the lifetime of
 * the in-flight promise — once resolved, a fresh identical request will
 * fire (which is correct for retries).
 */

(function () {
  'use strict';

  var _inFlight = {};

  /**
   * Build a dedupe key for method + path + body.
   * @param {string} method
   * @param {string} path
   * @param {*} body
   * @returns {string}
   */
  function _key(method, path, body) {
    return method + '|' + path + '|' + JSON.stringify(body);
  }

  function _request(method, path, body) {
    var key = _key(method, path, body);

    // If an identical request is already in-flight, share its promise.
    if (_inFlight[key]) {
      return _inFlight[key];
    }

    var opts = { method: method };
    if (body !== undefined && body !== null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }

    var task = fetch(path, opts)
      .then(function (res) {
        if (!res.ok) {
          return res
            .json()
            .catch(function () {
              return null; // non-JSON error body (HTML error page, empty, etc.)
            })
            .then(function (errData) {
              var msg =
                (errData && (errData.detail || errData.error)) ||
                res.statusText ||
                'Request failed';
              window.dispatchEvent(
                new CustomEvent('toast:show', {
                  detail: { message: msg, level: 'error' },
                })
              );
              throw new Error(msg);
            });
        }
        return res.json().catch(function () {
          return null; // 204 No Content or empty body
        });
      })
      .finally(function () {
        delete _inFlight[key];
      });

    _inFlight[key] = task;
    return task;
  }

  /**
   * Show a global toast (rendered by base.html's toastStack).
   * @param {string} message
   * @param {{level?: 'ok'|'info'|'warn'|'error', action?: {label: string, onClick: Function}}} [opts]
   */
  window._ssToast = function (message, opts) {
    window.dispatchEvent(
      new CustomEvent('toast:show', { detail: Object.assign({ message: message }, opts || {}) })
    );
  };

  window.api = {
    get: function (path) {
      return _request('GET', path);
    },
    post: function (path, body) {
      return _request('POST', path, body);
    },
    patch: function (path, body) {
      return _request('PATCH', path, body);
    },
    delete: function (path) {
      return _request('DELETE', path);
    },
  };
})();
