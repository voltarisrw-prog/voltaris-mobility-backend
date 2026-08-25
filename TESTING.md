# Testing

```
pytest tests/unit tests/api tests/security      # 57 tests, ~15s, no database
MONGODB_TEST_URI=... pytest tests/integration   # 6 tests, needs a real MongoDB
```

## The limitation that matters most

The default suite runs against `mongomock_motor`, an in-memory Motor double. It
exercises every line of service and route logic, and it does **not**:

- enforce unique indexes
- enforce partial indexes
- support multi-document transactions
- produce query execution plans

So three of the system's most important guarantees are **not** proven by the fast
suite, however green it is:

1. at most one active order per vehicle
2. no duplicate provider event can be processed twice
3. exactly one commission row per order

These live in `tests/integration/test_index_contract.py`, which is skipped when
`MONGODB_TEST_URI` is unset and runs against a real `mongo:7` service in CI. A green
local run is not sufficient evidence to ship.

This was not theoretical. `test_duplicate_webhook_does_not_pay_twice` initially failed
with a 409 instead of a 200 — the unique index did not fire under the mock, and the
state machine caught the replay instead. That surfaced a real weakness (the replay
guard relied solely on the index) and led to the read-check layer now in front of it.

## Coverage

**Money** — the worked example from the brief, the sum invariant, fee attribution,
both refusal conditions, and a thousand-sale drift check that a float implementation
would fail.

**State machines** — terminal states are terminal, orders cannot skip payment, no
state transitions to itself, `PAID` only refunds. Plus a tripwire asserting that
`ACTIVE_ORDER_STATUSES` and the partial index filter agree; if they drift, the
concurrency guard silently stops covering a status.

**Security primitives** — Argon2 output, distinct salts, refresh-as-access rejection,
expiry, `alg: none`, tampered payloads, wrong webhook secret, modified body, stale
timestamp, and a timestamp-swap forgery.

**Auth API** — registration, role self-assignment blocked, enumeration resistance,
lockout, logout revocation, refresh rotation with reuse detection killing the session,
suspended user with a valid token, and an assertion that no error response contains a
traceback, a hash, a driver name, or an internal path.

**Orders** — price ignores client input, idempotent replay, key reuse with a different
body rejected, keys scoped per user, reservation blocking a second buyer, ten
concurrent checkouts producing one winner, cancellation releasing the vehicle,
cross-customer read and checkout-hijack both 404, pagination, and an assertion that no
internal price, note, or commission appears in a response.

**Payments** — full happy path through to a settled commission, the three shapes of
"the frontend declares success", unsigned and wrongly-signed webhooks, duplicate
delivery, tampered amount, mismatched currency, unknown payment, a second success
event on an already-paid payment, and a failed payment leaving the order unpaid.

## Not yet covered

- Load and soak testing. No numbers have been measured; any performance claim in these
  documents is reasoning from the index design, not observation.
- MFA, since it is not implemented.
- Refund initiation, since the endpoint is not built.
- Provider timeout and reconciliation, which needs a real adapter to test against.
