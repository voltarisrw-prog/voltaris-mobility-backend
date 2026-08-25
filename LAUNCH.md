# Launch

## The honest position

**Launch 1 — the marketplace — is ready.** Browse, search, filter, compare,
vehicle detail, enquiries, test drives, dealers, blog, guides, charging,
accounts, saved vehicles. That is a lead-generation business and it works
end to end.

**Launch 2 — transactions — is not.** Four things stand between here and taking
money, and shipping without them means a site that looks finished and fails at
the moment it matters.

| Blocker | What happens today | Needed |
| --- | --- | --- |
| No payment provider | `get_provider()` refuses to start in production. Checkout is dead. | An adapter implementing `PaymentProvider`, plus webhook secret |
| No email transport | No reset or verification endpoints exist. A forgotten password locks the account permanently. | An email service, then `/auth/forgot-password`, `/auth/reset-password`, `/auth/verify-email` |
| `POST /seller-listings` missing | The Sell form collects everything, uploads photos, then 404s | The listing submission endpoint |
| MFA modelled, not built | SUPER_ADMIN reads every collection including internal pricing, behind a password alone | TOTP verification |

Each is hidden behind a feature flag rather than left broken. All flags default
off, so a missing environment variable hides a capability instead of exposing a
dead one.

Also still open: rate limiting covers only login (general throttling needs a
shared store), and order creation has a small non-atomic window documented in
`DATABASE.md`.

## Before Launch 1

**Legal.** `src/content/legal.ts` carries a working draft of the terms and
privacy policy, written to be accurate about what the platform actually does. It
is not legal advice and has TODO markers where the business must decide —
retention periods, and the RDB registration number. Have it reviewed against
Rwanda's Law No. 058/2021 on personal data protection before launch. You are
collecting names, phone numbers, and email addresses; this is not optional.

**Content.** The seed data is fictional. Do not launch with eight invented BYDs
and a dealer that does not exist. Real listings, or a coming-soon page.

**Domain and TLS.** `voltaris.rw` to the frontend, `api.voltaris.rw` to Fly.
Both hosts issue certificates automatically; TLS is not something to configure
by hand here.

## Deploying the backend

Fly.io, region **jnb** (Johannesburg). Worth stating why: Fly has no African
region closer to Kigali, and jnb is roughly 60–80ms away where Frankfurt is
150–190ms. Every API call pays that round trip, and server-rendered pages pay it
before anything appears. Hosting in Europe because the tooling defaults there
would make the site measurably slower for the people it is for.

```bash
fly launch --no-deploy --name voltaris-api --region jnb

fly secrets set \
  MONGODB_URI="mongodb+srv://..." \
  JWT_SECRET="$(openssl rand -base64 48)" \
  PAYMENT_WEBHOOK_SECRET="$(openssl rand -base64 32)" \
  CORS_ORIGINS='["https://voltaris.rw"]' \
  R2_ACCOUNT_ID="..." R2_ACCESS_KEY_ID="..." R2_SECRET_ACCESS_KEY="..." \
  R2_BUCKET="voltaris-media" R2_PUBLIC_BASE_URL="https://media.voltaris.rw" \
  SENTRY_DSN="https://...@sentry.io/..."

fly deploy
fly certs add api.voltaris.rw
```

The app **refuses to start** in production with a placeholder JWT secret, a
secret shorter than 32 characters, a localhost Mongo URI, or wildcard CORS. That
is deliberate: a backend that boots with a default signing key is worse than one
that fails loudly, because nobody notices until it is exploited.

Then prove it:

```bash
python scripts/smoke.py https://api.voltaris.rw
```

## Deploying the frontend

Vercel, since it is a Next.js app and the image optimiser and streaming SSR work
without configuration.

```
NEXT_PUBLIC_SITE_URL=https://voltaris.rw
NEXT_PUBLIC_API_BASE_URL=https://api.voltaris.rw/api/v1
NEXT_PUBLIC_MEDIA_BASE_URL=https://media.voltaris.rw
NEXT_PUBLIC_ENVIRONMENT=production
NEXT_PUBLIC_FEATURE_CHECKOUT=false
NEXT_PUBLIC_FEATURE_SELLER_LISTINGS=false
NEXT_PUBLIC_FEATURE_PASSWORD_RESET=false
NEXT_PUBLIC_FEATURE_RENTALS=false
```

`NEXT_PUBLIC_*` is compiled into the bundle, so changing a flag needs a rebuild,
not a restart.

`NEXT_PUBLIC_ENVIRONMENT` also controls robots.txt: anything other than
`production` serves `Disallow: /`. Preview deployments therefore cannot be
indexed by accident — a staging site competing with production for the same
keywords is very hard to undo.

## Atlas

- Cluster in the closest available region to jnb.
- **Network access**: allow Fly's egress, not `0.0.0.0/0`.
- Database user scoped to the `voltaris` database only — not an admin user.
- Continuous backup on, point-in-time within 7 days.
- **Rehearse a restore before launch, not after.** An untested backup is a
  belief, not a control.

## After deploying

1. `https://api.voltaris.rw/readyz` → `{"status":"ready","indexes":"ok"}`
2. `https://api.voltaris.rw/docs` → **404**. Docs are disabled in production.
3. `python scripts/smoke.py https://api.voltaris.rw` → 21 checks pass
4. `https://voltaris.rw/robots.txt` → crawlable, sitemap listed
5. Submit `sitemap.xml` in Search Console
6. Confirm `/admin` and `/account` return `X-Robots-Tag: noindex`
7. Trigger a test error and confirm it reaches Sentry

## Alerts worth wiring on day one

Three have no equivalent in a generic dashboard and must be set up explicitly:

| Signal | Why |
| --- | --- |
| `payment.amount_mismatch` | Any occurrence. Tampering or a provider bug. Page immediately. |
| `commission.needs_review` | A sale settled that could not be split. Money is unallocated. |
| `auth.refresh_reuse_detected` | A refresh token was replayed. Possible credential theft. |

Plus the ordinary ones: 5xx rate, p99 latency, database latency, webhook
signature failures.
