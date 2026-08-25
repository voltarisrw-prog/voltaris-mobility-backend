# Operations

## Backup and recovery

Atlas continuous backup: point-in-time within 7 days, daily snapshots retained 30.

**Restore must be rehearsed quarterly** into a scratch cluster, timed, and the result
recorded. A backup is not reliable until a restore has been tested; an untested backup
is a belief, not a control.

Procedure: restore the snapshot to a new cluster, point a staging instance at it,
confirm `/readyz`, and verify that recent orders, payments, and commissions are
present and that `owner_settlement + agency_commission == gross_sale` still holds
across the commission collection.

## Runbooks

### The container is running old code

Symptom: an endpoint that exists in the source returns 404, or the startup log's
`indexes` object lists fewer collections than the source defines (currently 20).

`docker compose up` reuses an existing image. It does **not** rebuild when `app/`
changes — only `docker compose watch` syncs source, and only while it is running.
A container started days ago from a stale image looks completely healthy.

```bash
docker compose up -d --build
```

`scripts/dev.sh` always passes `--build` for this reason.

### `dependency failed to start: container ... mongo-1 is unhealthy`

Look for this pair in the mongo logs:

```
"This node is not a member of the config"
Replica set state transition: STARTUP -> REMOVED
ReadConcernMajorityNotAvailableYet
```

The replica set config on the volume names a container that no longer exists.
`rs.initiate()` without an explicit member host records the container's random ID;
the volume outlives the container, the ID changes on recreate, and the node is
removed from its own replica set. It never elects a primary again, so the
healthcheck can never pass.

Fixed by `hostname: mongo` plus an explicit member host in the initiate call. To
clear a volume already holding a bad config:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-db.yml down -v
```

`-v` deletes the volume. It is local development data; on a real deployment,
reconfigure with `rs.reconfig(cfg, {force: true})` instead.

### The app is talking to the wrong database

Check what the container actually received, not what the .env says:

```bash
docker compose exec api env | grep MONGODB
```

Compose `environment:` literals beat `.env`. Every value in `docker-compose.yml`
uses `${VAR:-default}` so your `.env` wins; a bare literal there would silently
override a real Atlas URI.

### Payments stopped settling

1. Check webhook signature failures. A burst means the secret was rotated on one side.
2. Check `payment_events` for unprocessed rows: `{"processed": false}`.
3. Check the provider dashboard for delivery failures and replay from there — replay is
   safe, gate 2 makes it a no-op.
4. Never mark a payment PAID by hand. Fix delivery and let the webhook run.

### A commission is in NEEDS_REVIEW

The customer paid and the split could not be computed — almost always a listing whose
`seller_expected_price` exceeds the agency price, or a spread thinner than the agreed
rate. `review_reason` on the row states which. Finance corrects the listing and books
the split manually. The payment itself is sound and must not be reversed.

### Refresh token reuse detected

`auth.refresh_reuse_detected` in the audit log. The session is already dead. Contact
the account holder; if they did not trigger it, force a password reset and review
their recent orders.

### A vehicle is stuck RESERVED

An order holds it. Find it: `db.orders.find({vehicle_id, status: {$in: [...]}})`.
Cancelling that order releases the vehicle automatically. Do not edit the vehicle
status directly — the order would still hold the unique-index slot.

## Data retention

Audit logs have no TTL and are retained deliberately. Sessions, login attempts, and
idempotency keys expire via TTL indexes. Deleting audit history is an explicit
operational decision requiring sign-off, never a config change.
