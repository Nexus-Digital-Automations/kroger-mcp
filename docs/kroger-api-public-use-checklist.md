# Kroger API — public/multi-user use verification

> **Do this FIRST, before building hosting or hardening.** The whole "public app"
> plan rests on one question: *does Kroger permit you to serve unrelated
> third-party end-users through your single registered developer app?* If the
> answer is no (or "only under a commercial agreement"), it reshapes everything
> below it. This is a legal/ops action — not something the code can settle.

## Findings (researched 2026-06-23, Claude)
> Sources: Kroger Developer portal (developer.kroger.com), the `CupOfOwls/kroger-api`
> client library this project depends on, and Kroger API search results. The
> developer Terms/Acceptable-Use pages are JavaScript-only SPAs that could not be
> rendered headlessly (and web.archive.org is blocked from this environment), so
> the **exact ToS prose on commercial/multi-user serving is NOT yet confirmed** —
> see "Still needs you" below.

**Confirmed (evidence):**
- **Rate limits are per-app, per-day** ("a daily rate limit applied equally across
  all clients"):
  - Products API: **10,000 calls/day**
  - Cart API: **5,000 calls/day**
  - Locations API: **1,600 calls/day per endpoint**
  - Identity API: **5,000 calls/day**
  - Limits reset 24h after the first call. *Partner* APIs reportedly have no rate
    limit — but those require a separate partner agreement.
- **The Cart API is designed to act on behalf of an individual authenticated
  customer** (`cart.basic:write` scope, per-user OAuth). This is exactly how the
  app already works (per-user encrypted tokens) — so the **technical model is
  aligned with Kroger's intent**, not a hack.
- Kroger describes the public APIs as "available for all clients to build new
  products, services, or customer experiences" — building a product on them is
  anticipated.

**The binding practical constraint = the Products 10k/day cap (shared app).**
Rough math: if a typical active user triggers ~50–150 product calls/day (recipe
ingredient linking + browsing), the single shared app supports only **~roughly
70–200 active users/day** before throttling — *before* Redis caching, which the
app already does (products cached 1h, shared across users by `client_id`), so the
real ceiling is higher but still finite. Cart (5k/day) and Locations (the app uses
one fixed store) are not the bottleneck. **This cap, not server capacity, is what
limits how "public" the shared-app model can go** without a higher tier from Kroger.

**Unconfirmed / conflicting:** one secondary result mentioned "may not …
distribute, resell or otherwise use … for any commercial purpose," but that
appeared to reference a **consumer Kroger-app EULA**, not the developer API terms —
so it is NOT reliable evidence about the developer agreement. Do not treat it as
settled either way.

**Still needs you (cannot be done from public pages):**
1. Read the developer **Terms of Service** + **Acceptable Use** while logged in at
   developer.kroger.com (they render in a real browser) and confirm the
   multi-user / commercial / redistribution clauses.
2. If ambiguous, send the support questions below to get it in writing, and ask
   for a **higher Products rate tier** for a multi-user app.

## Why this is the gating item
- Today **every user shares one Kroger developer app** (`KROGER_CLIENT_ID` /
  `KROGER_CLIENT_SECRET`). Power users *can* bring their own credentials, but the
  default — and the only realistic "public self-serve" path — is the shared app.
- That shared app has **one rate-limit bucket**. Caching (Redis) + 429 backoff
  exist (`src/kroger_mcp/tools/_kroger_retry.py`), but they delay, not prevent,
  quota exhaustion as concurrent users grow.
- Each end-user *does* authenticate to **their own** Kroger account via OAuth
  (per-user encrypted tokens in `kroger_tokens`) — so the data model is clean.
  The open question is purely whether Kroger's **developer terms** allow your one
  app to broker that for the public.

## What to confirm in Kroger's Terms of Service
Read the current terms at the developer portal (Manage Apps → Terms, and the API
Terms of Service / Developer Agreement linked from
`https://developer.kroger.com/`). Look specifically for clauses on:

- [ ] **Third-party / end-user serving** — may your app act on behalf of users
      who are *not you*? (Many retailer dev terms restrict the app to the
      registered developer's own use.)
- [ ] **Redistribution / resale of API access** — is brokering Kroger data/actions
      to other people prohibited or gated behind a commercial license?
- [ ] **Commercial use** — is a non-commercial restriction in play? Does going
      "public" count as commercial even if free?
- [ ] **Rate limits / quotas** — the actual documented numbers (calls/sec,
      calls/day) for a standard app, and whether a higher tier exists.
- [ ] **End-user data handling** — requirements for storing user tokens, PII, and
      whether you must present a privacy policy / specific disclosures.
- [ ] **OAuth scopes** — confirm `cart.basic:write` (and product/profile scopes)
      are permitted for multi-user, not just personal, use.
- [ ] **Branding / attribution** — required "Powered by Kroger" or logo usage
      rules if you surface their data publicly.

## Questions to send Kroger Developer Support
> Suggested email/ticket to `https://developer.kroger.com` support. Adjust tone as
> you like — the goal is a written answer you can rely on.

1. "I've built an application that lets each end-user connect **their own** Kroger
   account via OAuth (authorization-code + PKCE) and place items in their own
   cart. I want to offer it publicly. Is it permitted to serve multiple unrelated
   end-users through a **single registered developer app**, or must each user
   register their own app?"
2. "What are the documented **rate limits** for a standard app, and is a **higher
   rate tier** available for an app serving many concurrent users? What's the
   process to request it?"
3. "Are there **data-handling or privacy-policy requirements** I must meet to
   store users' OAuth tokens and surface their cart/order data?"
4. "Is there any **commercial / redistribution agreement** required before I make
   this available to the public (free or paid)?"
5. "Are there **branding/attribution** requirements for an app that displays
   Kroger product and pricing data to end-users?"

## Decision matrix — what each answer means
| Kroger says… | Impact on the plan |
|---|---|
| ✅ Yes, one app may serve public users, here's your rate tier | Proceed with the full plan; record the rate tier and set per-user in-app quotas under it. |
| ⚠️ Allowed only under a commercial/partner agreement | Pause public launch; pursue the agreement first. Invite-only beta with *known* users may be defensible in the interim — confirm. |
| ⚠️ Each user must use their **own** app credentials | Not a true public self-serve product. Pivot to "power-user / BYO-credentials" positioning, or keep it invite-only for people willing to register their own Kroger app. |
| ❌ No third-party serving | Public launch is off the table under current terms. Keep it personal/family (Tailscale already covers that) or seek an enterprise relationship. |

## Interim risk control while you wait
- The **invite-only beta** (Cloudflare Access, allowlisted emails) keeps usage to
  a handful of people you know — far lower Kroger-quota pressure and a defensible
  "personal testing" footing than opening registration to strangers.
- Add per-user in-app quotas (e.g. N cart-adds/day) before *any* public exposure
  so one user can't drain the shared bucket — captured in the hardening spec.

---
*Owner: project maintainer. This is a pre-launch gate; the hardening + Cloudflare
work in `specs/public-launch-hardening.md` is ready to execute once this clears.*
