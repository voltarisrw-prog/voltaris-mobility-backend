# Voltaris Mobility — Backend

FastAPI modular monolith on MongoDB. Python 3.12.

## Status

**45 endpoints. Every route the frontend calls now exists.** Authentication and RBAC,
vehicles and faceted search, dealers, leads, the account area, editorial, charging,
orders, payments, commissions, audit, and the super-admin console.

Verified rather than asserted: `tests/api/test_contract.py` asserts the exact field
names and shapes the frontend's TypeScript interfaces destructure, because a renamed
key produces `undefined` at runtime rather than a type error.

Still not built: seller listing submission with media uploads, password reset and
email verification delivery, admin reporting beyond the console, and rentals with a
real availability calendar.

## Seeding

```bash
python scripts/seed.py
```

Eight vehicles, a dealer, two guides, two blog posts, a charging location, and a
super admin (`root@voltaris.rw` / `development-only-password`). It refuses to run
against production.

## Running it

```bash
cp .env.example .env      # set MONGODB_URI
./scripts/dev.sh
```

Builds, starts, waits for readiness, seeds, and smoke-tests in order. Full detail
and the step-by-step version are in **`RUNBOOK.md`**.

Do not paste the individual commands as a block: `docker compose up` never exits,
so anything after it stays queued behind it.

## Tests

```bash
pytest tests/unit tests/api tests/security      # 57 tests, no database needed
MONGODB_TEST_URI=mongodb://localhost:27017 pytest tests/integration
```

The second command matters. The default suite runs against an in-memory Mongo double
that does **not** enforce unique or partial indexes and has no transactions, so the
guarantees that depend on the database — one active order per vehicle, no duplicate
webhook events, one commission per order — are proven only by `tests/integration`.
CI runs them against a real MongoDB. See `TESTING.md`.

## The one thing to understand

No client-reachable endpoint can set a payment to PAID. The only path is a signed
provider webhook that passes six independent gates: signature, replay, order match,
amount match, currency match, and a legal state transition. Full detail in
`PAYMENTS.md`.

## Documents

- `ARCHITECTURE.md` — layering, module map, request lifecycle
- `DATABASE.md` — collections, every index and the query it serves, scaling notes
- `SECURITY.md` — authentication, RBAC matrix, the attack list and what stops each
- `PAYMENTS.md` — the money path, the six gates, failure modes
- `TESTING.md` — what is covered, and precisely what is not

## Roles

Registration always yields `BUYER`, server-assigned — a `roles` field in the request
body is ignored. Roles are re-read from the database on every request, so a change
takes effect immediately rather than at the next token expiry.

`SUPER_ADMIN` holds every permission, including `SYSTEM_INSPECT`. Note that `ADMIN`
does **not** hold `PAYMENT_REFUND`, `COMMISSION_WRITE`, `SETTLEMENT_WRITE`,
`ROLE_ASSIGN`, or `SYSTEM_INSPECT`: moving money, granting privilege, and reading raw
collections are separated from general administration on purpose.
- `DEPLOYMENT.md`, `OBSERVABILITY.md`, `OPERATIONS.md`, `ENVIRONMENT.md`, `API.md`
