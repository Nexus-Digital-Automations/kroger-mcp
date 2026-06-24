# Public launch — security hardening + Cloudflare edge

<!-- no-tasks -->
> **Status: PLAN (not yet executing).** This is the execution-ready blueprint for
> taking Smart Shopper from Tailnet-only to publicly accessible. It is
> intentionally opted out of Stop-gated tasks (`<!-- no-tasks -->`) because the
> build is **gated on Kroger ToS clearance** (`docs/kroger-api-public-use-checklist.md`)
> and rolls out in phases. Remove the `no-tasks` marker (or just start executing
> and check the boxes) when you give the go for a phase.

## Approval Trail
- 2026-06-23 — user asked "what do we need to make this app public?" Claude
  investigated deployment, Kroger integration, and security posture via three
  Explore agents. Grilled over two AskUserQuestion rounds.

## Decisions (locked from grilling)
- [ ] **Sequence:** verify Kroger ToS allows public multi-user serving FIRST
  (`docs/kroger-api-public-use-checklist.md`); build hosting + hardening only
  after it clears (or for an invite-only beta in the interim).
- [ ] **Launch posture:** invite-only beta behind Cloudflare Access first, then
  flip to fully-open self-serve once the epic + Kroger clearance are in.
- [ ] **Hosting:** Cloudflare Tunnel fronts the mini (no public IP / port-forward;
  free auto-TLS; DDoS/WAF; home IP hidden). Postgres/Redis stay localhost-bound.
- [ ] **Hardening depth:** full epic before opening registration to strangers.
- [ ] **Defense-in-depth:** Cloudflare at the edge (DDoS/WAF/bot/edge-rate-limit/
  Turnstile/Access) PLUS in-app hardening; neither replaces the other.

## Phasing
- **Phase 1 — Beta (gated public URL):** Cloudflare Tunnel + custom domain + TLS;
  Cloudflare Access allowlist gates the entire app; `/health` endpoint. The
  unhardened surface is invisible to the open internet during this phase.
- **Phase 2 — Hardening epic:** all app-level security items below, plus
  Cloudflare WAF + Bot Fight + edge rate-limiting + Turnstile wired but with
  Access still on.
- **Phase 3 — Open:** remove Cloudflare Access, Turnstile live on auth forms,
  per-user Kroger quotas enforced, Privacy/Terms published. Registration open to
  the public.

## Acceptance Criteria

### Edge / hosting (Cloudflare) — ops, mostly outside the repo
- [ ] Cloudflare Tunnel (`cloudflared`) runs on the mini as a managed service and
  proxies a custom domain to `127.0.0.1:8000`; the app no longer needs `0.0.0.0`
  exposure or any router port-forward.
- [ ] HTTPS is served end-to-end (Cloudflare edge cert + tunnel); plain-HTTP
  hits are redirected to HTTPS.
- [ ] Cloudflare Access gates the whole origin to an allowlist (email OTP) for the
  beta; documented toggle to disable for Phase 3.
- [ ] WAF managed ruleset + Bot Fight Mode enabled; edge rate-limiting rules on
  `/login` and `/register`.
- [ ] A documented `docs/cloudflare-setup.md` runbook captures tunnel config,
  Access policy, WAF/rate rules, and the Phase-3 cutover steps.

### App security hardening — code, in this repo
- [ ] **Secure session cookies:** `set_cookie(..., secure=True)` when serving over
  HTTPS, gated by env (e.g. `APP_ENV=prod` or a `COOKIE_SECURE` flag) so local
  HTTP dev still works. Covers both login and register handlers in
  `src/kroger_mcp/web/routes/auth.py`.
- [ ] **In-app rate limiting** on `/login`, `/register`, `/logout`, and the OAuth
  start endpoint — per-IP and per-email, backed by Redis, with escalating
  lockout. (Edge limits are belt; this is suspenders + works if edge is bypassed.)
- [ ] **CSRF protection** on all state-changing form POSTs (login, register,
  settings forms): server-issued token in the session, validated on POST.
- [ ] **Security-headers middleware** sets HSTS, `Content-Security-Policy`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and a
  `Referrer-Policy`. CSP is tuned to the app's real asset origins (incl. fonts).
- [ ] **Account-enumeration fix:** `/register` no longer reveals whether an email
  already exists (generic response or email-verification flow); login already
  returns a generic error.
- [ ] **Password policy:** raise minimum length and add a strength check
  (e.g. `zxcvbn`) on register and password change.
- [ ] **OAuth state hardening:** move the OAuth `state`/`code_verifier` out of the
  world-readable `/tmp/kroger_web_oauth_state.json` into per-user Redis with a
  short TTL.
- [ ] **Turnstile** (Cloudflare's CAPTCHA) on `/login` and `/register`, verified
  server-side; enabled for Phase 3 (no-op/bypass in dev).
- [ ] **Per-user Kroger quotas:** enforce a per-user daily cap on Kroger
  write/search calls so one user can't drain the shared app's rate bucket; cap
  value sourced from the rate tier Kroger confirms.
- [ ] **Privacy Policy + Terms pages** (`/privacy`, `/terms`) published and linked
  from register/footer; consent flow references the policy version.
- [ ] **Account deletion / data export:** a user can delete their account
  (cascading their data + revoking/forgetting Kroger tokens) and export their
  data — extends the existing consent-withdraw endpoints.

### Verification
- [ ] All new/changed code: `ruff check src/` clean; new auth/security logic
  (CSRF, rate-limit, cookie flags, enumeration) covered by unit tests under
  `tests/` (this is a security-critical domain).
- [ ] An e2e spec (`tests/e2e/`) proves: rate-limit kicks in after N bad logins;
  register doesn't leak email existence; security headers present on responses;
  Secure cookie flag set under prod config.
- [ ] Full python suite + e2e gate green before each phase's cutover.

## Notes / open items
- **Monitoring** (uptime + error tracking, e.g. a `/health` check pinged by an
  external monitor) is recommended before Phase 3 — scope it when we get there.
- **Secrets in prod** already come from Keychain/env (Fernet-encrypted Kroger
  tokens) — good. Re-verify no dev defaults leak into the prod `.env`.
- `purchase_events.user_id` / `orders.user_id` are nullable for legacy reasons;
  audit that no current write path leaves them unscoped before open launch.
