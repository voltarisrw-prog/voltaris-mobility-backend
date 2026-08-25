# Observability

## Structured logs

Every line is JSON with `ts`, `level`, `logger`, `message`, and `request_id`. The
request id lives in a `ContextVar`, so a line emitted five layers deep in a repository
carries the id of the request that caused it — the difference between a usable
production log and a pile of disconnected messages.

An inbound `X-Request-ID` is honoured so a trace survives across services; one is
generated when absent. It is echoed on every response.

`user_id` is attached once the principal resolves. Request lines carry `endpoint`,
`method`, `status`, and `duration_ms`.

### Redaction

The formatter itself drops `password`, `password_hash`, `token`, `access_token`,
`refresh_token`, `authorization`, `api_key`, `secret`, `webhook_secret`, `signature`,
`card`, and `cvv`, recursively through dicts and lists. Enforcing this in the
formatter rather than at call sites means a careless `extra={}` cannot leak a secret.

## Health

`/healthz` — liveness. Does **not** touch the database, deliberately: a slow Mongo
would otherwise cause the orchestrator to kill healthy pods and make an outage worse.

`/readyz` — readiness. Pings the database and reports `degraded` rather than raising,
so a load balancer can drain the instance without it crash-looping.

Startup is deliberately tolerant of an unreachable database: it logs the failure and
carries on serving as not-ready, rather than exiting. Crashing would mean the
orchestrator never gets to read `/readyz` at all — only a crash-loop — and locally it
would mean a reload during a Mongo restart takes the API down for good. Traffic is
still withheld, which is the actual objective. `/readyz` retries index creation once
the database appears and reports `indexes: ok` when it has converged.

## What to alert on

| Signal | Why |
| --- | --- |
| `payment.amount_mismatch` | Any occurrence. Tampering or a provider bug. Page immediately. |
| `commission.needs_review` | A sale settled that could not be split. Money is unallocated. |
| `auth.refresh_reuse_detected` | A refresh token was replayed. Possible credential theft. |
| Webhook signature failures | A burst means the endpoint is being probed. |
| Webhook processing errors | The provider will retry, then give up. Payments silently stop settling. |
| 5xx rate, p99 latency, database latency | Standard. |
| `ACCOUNT_LOCKED` rate | Credential stuffing. |

The first three are business-critical and have no equivalent in a generic dashboard —
they must be wired explicitly.

## Not built

Metrics export (Prometheus/OTel), tracing, and Sentry wiring. `SENTRY_DSN` is read
from configuration but nothing initialises the SDK yet.
