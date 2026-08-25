"""Index definitions, each with the query it exists to serve.

Every index costs write throughput and memory, so each one below names the access
pattern it supports. An index without a query in this file should be deleted.

Sizing note: the collections are designed to stay selective from 10k to 50M
documents. The vehicle list query is always bounded by `status` and sorted by an
indexed key, so it never scans; the compound orders are chosen so the equality
fields lead, the sort key follows, and range fields come last (the ESR rule).
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

from app.infrastructure.database.client import Collections

INDEXES: dict[str, list[IndexModel]] = {
    Collections.USERS: [
        # Login and registration both hit this. Unique, so two accounts can never
        # share an address even under a concurrent double-submit.
        IndexModel([("email", ASCENDING)], unique=True, name="uniq_email"),
        IndexModel([("phone", ASCENDING)], sparse=True, name="phone"),
        IndexModel([("roles", ASCENDING), ("status", ASCENDING)], name="roles_status"),
        # One account per external identity. Unique so a Google subject cannot end
        # up attached to two accounts, sparse because most users have none.
        IndexModel(
            [("identities.provider", ASCENDING), ("identities.subject", ASCENDING)],
            unique=True,
            sparse=True,
            name="uniq_identity",
        ),
    ],
    Collections.SESSIONS: [
        IndexModel([("session_id", ASCENDING)], unique=True, name="uniq_session"),
        IndexModel([("user_id", ASCENDING), ("revoked_at", ASCENDING)], name="user_active"),
        # Mongo reaps expired sessions itself; no cleanup job to forget to run.
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    Collections.LOGIN_ATTEMPTS: [
        IndexModel([("key", ASCENDING)], unique=True, name="uniq_key"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    Collections.OAUTH_STATES: [
        IndexModel([("state", ASCENDING)], unique=True, name="uniq_state"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    Collections.VEHICLES: [
        # The canonical public lookup: GET /vehicles/by-slug/{slug}.
        IndexModel([("slug", ASCENDING)], unique=True, name="uniq_slug"),
        # The marketplace list. Equality on status, then the default sort key.
        IndexModel(
            [("status", ASCENDING), ("published_at", DESCENDING)],
            name="status_published",
        ),
        # Faceted browse: make/body/location are equality, price is the range field,
        # so it goes last (ESR). Covers /brands/{make} and the category pages.
        IndexModel(
            [
                ("status", ASCENDING),
                ("make_slug", ASCENDING),
                ("body_type", ASCENDING),
                ("location.slug", ASCENDING),
                ("agency_price", ASCENDING),
            ],
            name="browse_facets",
        ),
        # Sort-by-range and the minRange filter.
        IndexModel([("status", ASCENDING), ("range_km", DESCENDING)], name="status_range"),
        IndexModel([("dealer_id", ASCENDING), ("status", ASCENDING)], name="dealer_inventory"),
        # Free-text search. Weighted so a make match outranks a description match.
        IndexModel(
            [("make", TEXT), ("model", TEXT), ("variant", TEXT), ("description", TEXT)],
            weights={"make": 10, "model": 8, "variant": 4, "description": 1},
            name="text_search",
        ),
    ],
    Collections.DEALERS: [
        IndexModel([("slug", ASCENDING)], unique=True, name="uniq_slug"),
        IndexModel([("verified", ASCENDING), ("name", ASCENDING)], name="verified_name"),
    ],
    Collections.INQUIRIES: [
        IndexModel([("reference", ASCENDING)], unique=True, name="uniq_reference"),
        IndexModel(
            [("customer_id", ASCENDING), ("created_at", DESCENDING)], name="customer_inquiries"
        ),
        IndexModel(
            [("vehicle_id", ASCENDING), ("created_at", DESCENDING)], name="vehicle_inquiries"
        ),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="queue"),
        # Rate limiting the enquiry form: count recent submissions per email.
        IndexModel([("email", ASCENDING), ("created_at", DESCENDING)], name="email_recent"),
    ],
    Collections.TEST_DRIVES: [
        IndexModel([("reference", ASCENDING)], unique=True, name="uniq_reference"),
        IndexModel(
            [("customer_id", ASCENDING), ("created_at", DESCENDING)], name="customer_drives"
        ),
        IndexModel([("status", ASCENDING), ("preferred_date", ASCENDING)], name="schedule"),
    ],
    Collections.SAVED_VEHICLES: [
        # One save per person per vehicle; saving twice is idempotent, not a duplicate.
        IndexModel(
            [("user_id", ASCENDING), ("vehicle_id", ASCENDING)], unique=True, name="uniq_save"
        ),
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_saves"),
    ],
    Collections.SAVED_SEARCHES: [
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_searches"),
    ],
    Collections.NOTIFICATIONS: [
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_feed"),
        IndexModel([("user_id", ASCENDING), ("read", ASCENDING)], name="unread"),
    ],
    Collections.SELLER_LISTINGS: [
        IndexModel([("reference", ASCENDING)], unique=True, name="uniq_reference"),
        IndexModel([("status", ASCENDING), ("submitted_at", DESCENDING)], name="review_queue"),
        IndexModel([("email", ASCENDING), ("submitted_at", DESCENDING)], name="seller_history"),
    ],
    Collections.MEDIA: [
        IndexModel([("media_key", ASCENDING)], unique=True, name="uniq_media_key"),
        IndexModel([("owner_id", ASCENDING), ("created_at", DESCENDING)], name="owner_uploads"),
        # Finds intents that were presigned but never finalised, so the reaper can
        # delete the orphaned objects they point at.
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="pending_sweep"),
    ],
    Collections.ARTICLES: [
        IndexModel([("slug", ASCENDING)], unique=True, name="uniq_slug"),
        # `kind` leads because /guides and /blog always filter on it; the sort key
        # follows. Guides and posts share a store but never a listing query.
        IndexModel(
            [("kind", ASCENDING), ("status", ASCENDING), ("published_at", DESCENDING)],
            name="kind_status_published",
        ),
        IndexModel(
            [("kind", ASCENDING), ("category", ASCENDING), ("published_at", DESCENDING)],
            name="kind_category_published",
        ),
    ],
    Collections.CHARGING_LOCATIONS: [
        IndexModel([("slug", ASCENDING)], unique=True, name="uniq_slug"),
        IndexModel([("district", ASCENDING), ("name", ASCENDING)], name="by_district"),
        # For "chargers near me" once the frontend asks for it.
        IndexModel([("location", "2dsphere")], name="geo"),
    ],
    Collections.ORDERS: [
        IndexModel([("reference", ASCENDING)], unique=True, name="uniq_reference"),
        IndexModel(
            [("customer_id", ASCENDING), ("created_at", DESCENDING)], name="customer_orders"
        ),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="status_created"),
        # THE CONCURRENCY GUARD. At most one order may hold a vehicle in a
        # non-terminal state. The partial filter is what makes this possible: a
        # vehicle can be ordered again after a cancellation, but never twice at once.
        IndexModel(
            [("vehicle_id", ASCENDING)],
            unique=True,
            name="uniq_active_order_per_vehicle",
            partialFilterExpression={
                "status": {"$in": ["PENDING", "PAYMENT_PENDING", "PAID", "PROCESSING"]}
            },
        ),
    ],
    Collections.PAYMENTS: [
        IndexModel([("order_id", ASCENDING), ("created_at", DESCENDING)], name="order_payments"),
        IndexModel(
            [("provider", ASCENDING), ("provider_transaction_id", ASCENDING)],
            unique=True,
            sparse=True,
            name="uniq_provider_txn",
        ),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="status_created"),
    ],
    Collections.PAYMENT_EVENTS: [
        # Duplicate-webhook protection. The provider's event id is the natural key;
        # a unique index makes replay a write conflict rather than a race we lose.
        IndexModel(
            [("provider", ASCENDING), ("provider_event_id", ASCENDING)],
            unique=True,
            name="uniq_provider_event",
        ),
        IndexModel(
            [("payment_id", ASCENDING), ("received_at", ASCENDING)], name="payment_timeline"
        ),
    ],
    Collections.COMMISSIONS: [
        IndexModel([("order_id", ASCENDING)], unique=True, name="uniq_order"),
        IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="status_created"),
    ],
    Collections.IDEMPOTENCY: [
        # Scoped to the user so one client's key cannot collide with another's.
        IndexModel(
            [("user_id", ASCENDING), ("endpoint", ASCENDING), ("key", ASCENDING)],
            unique=True,
            name="uniq_scope_key",
        ),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expiry"),
    ],
    Collections.AUDIT_LOGS: [
        IndexModel(
            [("entity_type", ASCENDING), ("entity_id", ASCENDING), ("at", DESCENDING)],
            name="entity_history",
        ),
        IndexModel([("actor_id", ASCENDING), ("at", DESCENDING)], name="actor_history"),
        IndexModel([("action", ASCENDING), ("at", DESCENDING)], name="action_history"),
        # No TTL. Financial audit records are retained deliberately; deletion is a
        # documented operational decision, not an index side effect.
    ],
}


async def ensure_indexes(db: AsyncIOMotorDatabase) -> dict[str, int]:
    """Create every index. Idempotent, so it is safe on every deploy."""
    created: dict[str, int] = {}
    for collection, models in INDEXES.items():
        if models:
            await db[collection].create_indexes(models)
            created[collection] = len(models)
    return created
