# Database

MongoDB. Collections, and every index with the query it exists to serve — an index
without a query listed here should be deleted.

## Document boundaries

Embedded where the data is bounded and always read with its parent: vehicle images,
charging spec, location, order lines, the vehicle's `internal` financials.

Referenced where it grows without limit or is read independently: orders, payments,
payment events, commissions, audit logs, sessions.

The decision rule is growth. A vehicle has at most a few dozen images; a vehicle could
accumulate unbounded audit entries, so those are their own collection.

## `internal` as a subdocument

`seller_expected_price`, `commission_bps`, `internal_notes`, and `acquisition_cost`
live under `internal` rather than at the top level, so the public projection is
`{"internal": 0}` — one rule that cannot be partially forgotten, instead of a denylist
of field names that has to be extended every time someone adds a column.

## Indexes

### users
| Index | Serves |
| --- | --- |
| `email` unique | Login and registration. Unique, so a concurrent double-submit cannot create two accounts. |
| `phone` sparse | Lookup by phone; sparse because it is optional. |
| `roles, status` | Admin user lists. |

### sessions
| Index | Serves |
| --- | --- |
| `session_id` unique | Every authenticated request. |
| `user_id, revoked_at` | "End all my other sessions". |
| `expires_at` TTL | Mongo reaps expired sessions; no cleanup job to forget. |

### vehicles
| Index | Serves |
| --- | --- |
| `slug` unique | `GET /vehicles/by-slug/{slug}`. |
| `status, published_at desc` | The default marketplace list. |
| `status, make_slug, body_type, location.slug, agency_price` | Faceted browse and the category pages. Equality fields lead, the range field is last — the ESR rule. |
| `status, range_km desc` | Sort by range and the `minRange` filter. |
| `dealer_id, status` | Dealer inventory. |
| text on make/model/variant/description | Free-text search, weighted so a make match outranks a description match. |

### orders
| Index | Serves |
| --- | --- |
| `reference` unique | Customer-facing lookup. |
| `customer_id, created_at desc` | "My orders", cursor-paginated. |
| `status, created_at desc` | Admin queues. |
| `vehicle_id` unique **partial** | **The concurrency guard.** Filtered to `PENDING/PAYMENT_PENDING/PAID/PROCESSING`, so at most one order may hold a vehicle at a time, while a cancelled order releases it. |

### payments, payment_events, commissions
`(provider, provider_transaction_id)` unique sparse; `(provider, provider_event_id)`
unique — duplicate webhooks become a write conflict rather than a race we lose;
`order_id` unique on commissions — revenue cannot be booked twice.

### idempotency_keys
`(user_id, endpoint, key)` unique, scoped per user so one client's key cannot collide
with another's. TTL 24h.

### audit_logs
`(entity_type, entity_id, at)`, `(actor_id, at)`, `(action, at)`. **No TTL** —
financial audit retention is a deliberate operational decision, not an index side
effect.

## Scaling

Designed to stay selective from 10k to 50M documents. Every list query is bounded by
an equality field and sorted on an indexed key, so none degrades into a scan.

`skip` is not used anywhere. It costs O(offset) and is unusable past a few thousand
documents; list endpoints use cursor pagination on `created_at`.

`tests/integration` asserts the browse query's execution plan contains no `COLLSCAN`.
Beyond roughly 10M vehicles, sharding on `location.slug` is the likely next step —
Rwanda-then-region growth means queries are naturally geo-partitioned.

## Concurrency

Two mechanisms, deliberately different:

- **Optimistic locking** on documents with contended fields. Vehicles, orders, and
  payments carry a `version`; every write asserts the version it read and increments
  it. A losing writer gets `CONCURRENT_MODIFICATION`, never a silent overwrite.
- **Unique partial indexes** where the invariant spans documents. "One active order
  per vehicle" cannot be enforced by reading first — two requests can both read
  "available" before either writes. The database decides.

## Transactions

Not currently used. Order creation is insert-then-conditional-update with an explicit
rollback on failure, which is correct but not atomic: a crash between the two leaves
an order with an unreserved vehicle.

The window is small and self-healing on the next reconciliation pass, but a
`with session.start_transaction()` around the pair is the proper fix and is the first
thing to add. `docker-compose.yml` runs Mongo as a single-node replica set so the
local environment already supports it.

## Backup and recovery

Atlas continuous backup, point-in-time within 7 days, daily snapshots for 30. Restore
must be rehearsed quarterly into a scratch cluster — a backup is not reliable until a
restore has been tested. Procedure in `OPERATIONS.md`.
