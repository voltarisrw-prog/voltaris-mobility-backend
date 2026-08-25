# API

Base path `/api/v1`. OpenAPI at `/docs` and `/openapi.json` in development only.

## Conventions

Bearer token in `Authorization`. Errors always take one shape:

```json
{"success": false, "error": {"code": "VEHICLE_NOT_FOUND", "message": "…", "request_id": "req_82931"}}
```

The `request_id` is echoed in the `X-Request-ID` header and appears on every log line
for that request. Quote it in a support ticket.

## Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/auth/register` | — | `roles` and `status` in the body are ignored |
| POST | `/auth/login` | — | may return 401 `MFA_REQUIRED` |
| POST | `/auth/refresh` | — | rotates; replaying a burnt token kills the session |
| POST | `/auth/logout` | bearer | revokes the session server-side |
| GET | `/auth/session` | bearer | current principal |
| POST | `/orders` | bearer | accepts `Idempotency-Key` |
| GET | `/orders` | bearer | cursor-paginated, scoped to the caller |
| GET | `/orders/{id}` | bearer | 404 for another customer's order |
| POST | `/orders/{id}/checkout-session` | bearer | reuses an in-flight payment |
| GET | `/orders/{id}/payment` | bearer | reconciled state, never cached |
| POST | `/webhooks/payments` | signature | the only path that can set PAID |
| GET | `/healthz`, `/readyz` | — | liveness, readiness |

## Idempotency

`POST /orders` accepts `Idempotency-Key`. A repeat returns the original order rather
than creating a second. The body is fingerprinted alongside the key: reusing a key
with a different body returns 422 `IDEMPOTENCY_KEY_REUSED` rather than replaying
someone else's order. Keys are scoped per user and expire after 24 hours.

## Pagination

Cursor-based. `?limit=20&cursor=<iso8601>`; the response carries `next_cursor`, null
on the last page. No endpoint returns an unbounded collection, and `skip` is not used
anywhere.

## Webhooks

`POST /webhooks/payments` with header `X-Voltaris-Signature: t=<unix>,v1=<hex>`, an
HMAC-SHA256 over `<t>.<raw body>`. Tolerance 300 seconds.

Response contract: 200 for applied, duplicate, or ignored — anything the provider
should stop retrying. 4xx only for signature failure, unknown payment, or a mismatch
that must be investigated.
