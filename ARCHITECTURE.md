# Architecture

A modular monolith. One deployable, clear internal seams, no microservices — there is
no scaling, organisational, or reliability requirement that would justify the
distributed-transaction problem they would introduce into a payment flow.

## Layers

```
app/api/v1/routes/   HTTP only: schemas, status codes, dependency wiring
app/api/deps.py      identity, authorization guards, service construction
app/modules/*/       service.py  — business logic and invariants
                     models.py   — domain types and state machines
app/infrastructure/  database, payment provider, storage adapters
app/core/            config, errors, logging, security primitives
```

One rule holds it together: **routes contain no business logic, and services contain
no HTTP.** A route validates, calls one service method, and shapes the response. A
service never sees a `Request` and never raises an `HTTPException` — it raises
`AppError`, which the central handler translates.

External integrations sit behind a Protocol (`PaymentProvider`), so the money path can
be tested without a network and a provider swap touches one file.

## Request lifecycle

```
request
  -> observability middleware   request_id assigned, timer started
  -> security middleware        size limit, response headers
  -> route                      Pydantic validates the body
  -> current_principal          token verified, session checked, roles re-read
  -> permission guard           server-side authorization
  -> service                    invariants, state machines, persistence
  -> audit                      for anything financial or administrative
  <- response                   X-Request-ID attached, structured log emitted
```

Failures take one of three handlers: `AppError` becomes its mapped status and public
message, validation errors become a bare `INVALID_REQUEST` (Pydantic's error list can
echo a submitted password), and anything else is logged with a full traceback and
returned as `INTERNAL_ERROR`.

## Error handling

`AppError` carries a public `code` and an internal `detail`. The detail is logged and
never serialised — it may name a collection, an internal id, or a provider reference.
The client gets the code, a mapped message, and a request id, which is enough to
correlate with a log line on request.

## What is built

Auth, users/RBAC, vehicles (model and projection), orders, payments, commissions,
audit. Twelve endpoints.

## Remaining work

Not built, in rough dependency order:

1. **Vehicles CRUD and public search** — the model, indexes, and projection exist; the
   routes do not. Highest priority, since the frontend marketplace depends on it.
2. **Leads/CRM** with source attribution, and test drives.
3. **Media** — signed upload URLs, metadata validation, derivative generation.
4. **Content/CMS** with the draft/review/published/archived lifecycle.
5. **Notifications** and the background worker (email, SMS, image processing).
6. **Admin and finance reporting** — GMV, commission revenue, pending settlements.
7. **Rentals**, which needs an availability calendar rather than a single reservation.

## Deliberate omissions

- **No Redis.** Nothing has been measured that needs it. It will be needed for
  distributed rate limiting, which is the first honest justification.
- **No queue yet.** No operation currently blocks a request long enough to warrant one.
  Notifications will change that.
- **No transactions yet.** See `DATABASE.md` — a known, documented gap with a small
  window and a clear fix.


## The public read path

`VehicleService` is the widest-read code in the system, so two rules are enforced
structurally rather than by convention.

**The projection, not the serialiser, hides money.** Every public read applies
`{"internal": 0}` in the repository call. Seller expectations, commission rates, and
internal notes cannot reach a response even if a serialiser is later edited
carelessly, because they were never loaded. Asserted from the outside in
`test_internal_pricing_never_appears_in_a_public_response`.

**The wire format is the frontend's, not the database's.** Stored status is a
seven-state uppercase lifecycle; the API exposes four lowercase values. Translating
at the boundary is what keeps `DRAFT` and `PENDING_REVIEW` from ever appearing in a
payload — the visible set is a whitelist, so a new lifecycle state is invisible until
someone decides otherwise.

Filters accept the frontend's camelCase query keys (`minPrice`, `maxMileage`)
directly. Renaming them at the boundary would mean two vocabularies for one concept.

## Anonymous leads

Enquiries and test drives resolve the caller through `OptionalUser`, not
`CurrentUser`. Requiring an account before someone can ask a question loses exactly
the lead worth having; when a session *is* present the record is attached to it so
the person sees it later under "my enquiries".

The enquiry form carries a honeypot (`company_website`). A filled one returns a
normal-looking success and stores nothing, so probing teaches an operator nothing.
Behind it sits a real per-email hourly limit, because a bot that reads the form
properly will not fall for the honeypot.
