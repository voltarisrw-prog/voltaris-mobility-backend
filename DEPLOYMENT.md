# Deployment

## Pipeline

lint → format check → types → unit/API/security tests → **integration tests against a
real MongoDB** → dependency audit → static analysis → secret scan → image build →
non-root assertion → staging → production behind manual approval.

The integration job is not optional. It is the only stage that proves the unique
indexes, the partial index, and the concurrency guarantee.

`develop` deploys to staging automatically. `main` deploys to production only through
a GitHub environment approval. Nothing reaches production from a laptop.

## Image

Multi-stage. The build stage carries `build-essential` for `argon2-cffi` and is
discarded. Runtime is `python:3.12-slim`, runs as uid 1001 with no home directory and
`nologin` as its shell, and contains no secrets. CI fails the build if the image runs
as root.

`HEALTHCHECK` polls `/healthz` only — liveness, not readiness, so a slow database
cannot cause healthy containers to be restarted.

## First production deploy

1. Populate every variable in `ENVIRONMENT.md` from managed secret storage.
2. Set `PAYMENT_PROVIDER` to the real provider and implement its adapter. The stub is
   refused in production.
3. Register the webhook URL with the provider and set `PAYMENT_WEBHOOK_SECRET` to the
   signing secret it issues.
4. Poll `/readyz` until it returns `{"status": "ready", "indexes": "ok"}` before
   shifting traffic. `degraded` means the database is unreachable or indexes have not
   converged. The process stays up either way, so read the body rather than inferring
   health from the fact that it is running.
6. Confirm `/docs` and `/openapi.json` return 404 in production.

## Rollback

Images are immutable; redeploy the previous tag. There are no schema migrations to
reverse — MongoDB documents are additive, and no field has been removed. If a rollback
crosses an index change, indexes are additive too and the older image ignores them.
