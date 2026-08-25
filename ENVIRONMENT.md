# Environment

Every variable is in `.env.example`. Nothing has a usable production default.

## The production guard

`Settings` refuses to construct when `ENVIRONMENT=production` and any of these hold:

- `JWT_SECRET` is empty, still `dev-insecure-change-me`, or shorter than 32 characters
- `PAYMENT_WEBHOOK_SECRET` is empty or still the placeholder
- `MONGODB_URI` points at localhost
- `CORS_ORIGINS` contains `*`

The process exits at import. A backend that boots with a default JWT secret is worse
than one that fails loudly, because nobody notices until it is exploited. Verified
across all four cases plus the passing configuration.

`PAYMENT_PROVIDER=stub` is refused at provider construction in production for the same
reason — it would accept orders that can never be paid.

## Secrets

Database URI, JWT secret, payment API key, webhook secret, storage credentials,
monitoring DSN. In development these come from `.env`, which is gitignored. In staging
and production they come from managed secret storage and are injected as environment
variables at run time. None is baked into the image.

## Rotating the JWT secret

Rotation invalidates every access and refresh token, signing everyone out. Either
accept that during a maintenance window, or implement dual-key verification (accept
the old key for the refresh TTL, sign only with the new one) before rotating. Not
currently implemented.


## Dependency files

`requirements.txt` is the runtime set and is the only one the production image
installs. `requirements-dev.txt` pulls it in and adds `watchfiles`, pytest, ruff,
mypy, and mongomock.

The split matters: the image previously installed the full `pip freeze`, shipping
pytest, mongomock, ruff, and mypy into production — attack surface and megabytes for
no benefit. CI installs the dev set for tests and audits the **runtime** set with
`pip-audit`, so a CVE in a lint tool does not block a release for code that never
ships.
