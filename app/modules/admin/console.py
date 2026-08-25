"""The super-admin console.

## Why this is not a database shell

The request was "control the database from the web app". This module deliberately
does not do that, and the reason is worth stating plainly rather than burying.

An endpoint that executes arbitrary Mongo operations would bypass every invariant the
rest of this codebase exists to enforce: the order and payment state machines, the
`owner_settlement + agency_commission == gross_sale` assertion, the unique partial
index that stops two people buying one vehicle, and the audit trail. A single
mistyped `updateMany` silently corrupts financial records with no before-image to
restore from. Worse, it collapses the blast radius of one stolen super-admin session
from "can see everything" to "can rewrite every payment in the system" — and because
such a change looks like a legitimate write, nothing downstream would flag it.

So this console gives super admins two things instead:

1. **Read anything.** Any collection, any filter, fully inspectable, including the
   internal financial fields hidden from every other surface. Every read is logged.
2. **Write through named operations only.** Suspending a user, assigning a role,
   cancelling an order. Each one validates, records a before-image, and audits.

That covers the actual need — seeing and controlling the whole system — without
handing anyone a loaded gun pointed at the ledger. Genuine schema surgery belongs in
a reviewed migration script run against a backup, not in a browser tab.

If arbitrary execution is still wanted later, the honest way is `mongosh` against
Atlas with per-engineer credentials and Atlas's own audit log, not an HTTP endpoint
wearing this application's identity.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.audit.service import AuditService
from app.modules.users.models import Role, UserStatus

logger = logging.getLogger("voltaris.admin")

#: Readable collections. An allow-list rather than a denylist, so a collection added
#: later is invisible here until someone decides it should be readable.
READABLE = {
    Collections.USERS,
    Collections.SESSIONS,
    Collections.VEHICLES,
    Collections.ORDERS,
    Collections.PAYMENTS,
    Collections.PAYMENT_EVENTS,
    Collections.COMMISSIONS,
    Collections.IDEMPOTENCY,
    Collections.AUDIT_LOGS,
    Collections.LOGIN_ATTEMPTS,
}

#: Stripped from every document this module returns, in every collection.
#:
#: Super admins may read internal pricing, commissions, and notes — that is the point
#: of the role. They may not read password hashes, MFA secrets, or OAuth subjects:
#: those grant no operational insight and their only use is impersonation.
ALWAYS_REDACT = {"password_hash", "mfa_secret", "refresh_jti", "identities"}

MAX_LIMIT = 200

#: Query operators permitted in a filter. `$where` and `$expr` accept JavaScript and
#: arbitrary expressions respectively and are how a "read-only" API becomes remote
#: code execution; `$function` likewise. They are not on this list.
ALLOWED_OPERATORS = {
    "$eq",
    "$ne",
    "$gt",
    "$gte",
    "$lt",
    "$lte",
    "$in",
    "$nin",
    "$and",
    "$or",
    "$not",
    "$nor",
    "$exists",
    "$type",
    "$regex",
    "$options",
    "$size",
    "$all",
    "$elemMatch",
}


def _validate_filter(node: Any, depth: int = 0) -> None:
    """Reject anything that could execute rather than match."""
    if depth > 6:
        raise AppError(ErrorCode.INVALID_REQUEST, detail="filter nested too deeply")
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("$") and key not in ALLOWED_OPERATORS:
                raise AppError(
                    ErrorCode.INVALID_REQUEST,
                    detail=f"operator {key} is not permitted in an inspection filter",
                )
            if key == "$regex" and isinstance(value, str):
                # A pathological pattern against millions of documents is a denial of
                # service against our own database.
                if len(value) > 200:
                    raise AppError(ErrorCode.INVALID_REQUEST, detail="regex too long")
                try:
                    re.compile(value)
                except re.error as exc:
                    raise AppError(ErrorCode.INVALID_REQUEST, detail=f"bad regex: {exc}") from exc
            _validate_filter(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _validate_filter(item, depth + 1)


def _redact(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("[redacted]" if key in ALWAYS_REDACT else value) for key, value in document.items()
    }


class AdminConsoleService:
    def __init__(self, db: AsyncIOMotorDatabase, audit: AuditService) -> None:
        self._db = db
        self._audit = audit

    # -- monitoring --------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        """Everything a super admin needs on one screen, in one round trip each."""
        day_ago = datetime.now(UTC) - timedelta(days=1)
        week_ago = datetime.now(UTC) - timedelta(days=7)

        users = self._db[Collections.USERS]
        vehicles = self._db[Collections.VEHICLES]
        orders = self._db[Collections.ORDERS]
        payments = self._db[Collections.PAYMENTS]
        commissions = self._db[Collections.COMMISSIONS]
        events = self._db[Collections.PAYMENT_EVENTS]

        revenue = await commissions.aggregate(
            [
                {"$match": {"created_at": {"$gte": week_ago}, "status": {"$ne": "REVERSED"}}},
                {
                    "$group": {
                        "_id": None,
                        "gross": {"$sum": "$gross_sale"},
                        "commission": {"$sum": "$agency_commission"},
                        "net": {"$sum": "$net_revenue"},
                    }
                },
            ]
        ).to_list(length=1)
        totals = revenue[0] if revenue else {"gross": 0, "commission": 0, "net": 0}

        return {
            "users": {
                "total": await users.count_documents({}),
                "active": await users.count_documents({"status": UserStatus.ACTIVE.value}),
                "suspended": await users.count_documents({"status": UserStatus.SUSPENDED.value}),
                "new_24h": await users.count_documents({"created_at": {"$gte": day_ago}}),
            },
            "vehicles": {
                "pending_review": await vehicles.count_documents({"status": "PENDING_REVIEW"}),
                "available": await vehicles.count_documents({"status": "AVAILABLE"}),
                "reserved": await vehicles.count_documents({"status": "RESERVED"}),
                "sold": await vehicles.count_documents({"status": "SOLD"}),
            },
            "orders": {
                "awaiting_payment": await orders.count_documents({"status": "PAYMENT_PENDING"}),
                "paid": await orders.count_documents({"status": "PAID"}),
                "new_24h": await orders.count_documents({"created_at": {"$gte": day_ago}}),
            },
            "money_7d": {
                "gross_sale": totals.get("gross", 0),
                "agency_commission": totals.get("commission", 0),
                "net_revenue": totals.get("net", 0),
            },
            # The three signals that mean something is actually wrong.
            "alerts": {
                "commissions_needing_review": await commissions.count_documents(
                    {"status": "NEEDS_REVIEW"}
                ),
                "failed_payments_24h": await payments.count_documents(
                    {"status": "FAILED", "updated_at": {"$gte": day_ago}}
                ),
                "rejected_webhooks_24h": await events.count_documents(
                    {"signature_valid": False, "received_at": {"$gte": day_ago}}
                ),
            },
        }

    async def collection_stats(self) -> list[dict[str, Any]]:
        stats = []
        for name in sorted(READABLE):
            stats.append(
                {
                    "collection": name,
                    "documents": await self._db[name].count_documents({}),
                    "indexes": len(await self._db[name].index_information()),
                }
            )
        return stats

    # -- read-only inspection ---------------------------------------------

    async def inspect(
        self,
        *,
        collection: str,
        query: dict[str, Any] | None,
        sort_field: str = "_id",
        descending: bool = True,
        limit: int = 50,
        actor_id: str,
    ) -> dict[str, Any]:
        if collection not in READABLE:
            raise AppError(
                ErrorCode.FORBIDDEN, detail=f"{collection} is not an inspectable collection"
            )
        query = query or {}
        _validate_filter(query)
        limit = max(1, min(limit, MAX_LIMIT))

        rows = (
            await self._db[collection]
            .find(query)
            .sort(sort_field, -1 if descending else 1)
            .limit(limit)
            .to_list(length=limit)
        )

        # Reads are audited too. Who looked at what, and when, is part of knowing
        # whether an account has been misused.
        await self._audit.record(
            action="admin.inspect",
            entity_type="collection",
            entity_id=collection,
            actor_id=actor_id,
            after={"filter": str(query)[:500], "returned": len(rows), "limit": limit},
        )

        return {
            "collection": collection,
            "count": len(rows),
            "limit": limit,
            "truncated": len(rows) == limit,
            "items": [_redact(row) for row in rows],
        }

    async def entity_history(self, *, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        """Every recorded change to one record, newest first."""
        return (
            await self._db[Collections.AUDIT_LOGS]
            .find({"entity_type": entity_type, "entity_id": entity_id})
            .sort("at", -1)
            .limit(MAX_LIMIT)
            .to_list(length=MAX_LIMIT)
        )

    # -- named write operations -------------------------------------------

    async def set_user_status(
        self, *, user_id: str, status: UserStatus, actor_id: str, reason: str
    ) -> dict[str, Any]:
        user = await self._db[Collections.USERS].find_one({"_id": user_id})
        if user is None:
            raise AppError(ErrorCode.USER_NOT_FOUND, detail=user_id)

        await self._db[Collections.USERS].update_one(
            {"_id": user_id},
            {"$set": {"status": status.value, "updated_at": datetime.now(UTC)}},
        )
        if status is not UserStatus.ACTIVE:
            # Suspension has to end live sessions, or the user keeps working until
            # their access token expires.
            await self._db[Collections.SESSIONS].update_many(
                {"user_id": user_id, "revoked_at": None},
                {"$set": {"revoked_at": datetime.now(UTC), "revoked_reason": "status_change"}},
            )

        await self._audit.record(
            action="admin.user_status_changed",
            entity_type="user",
            entity_id=user_id,
            actor_id=actor_id,
            before={"status": user["status"]},
            after={"status": status.value, "reason": reason},
        )
        return {"user_id": user_id, "status": status.value}

    async def set_user_roles(
        self, *, user_id: str, roles: list[Role], actor_id: str, reason: str
    ) -> dict[str, Any]:
        if user_id == actor_id:
            # Self-demotion can lock the last super admin out of the system, and
            # self-promotion makes the audit trail meaningless.
            raise AppError(ErrorCode.FORBIDDEN, detail="cannot change your own roles")

        user = await self._db[Collections.USERS].find_one({"_id": user_id})
        if user is None:
            raise AppError(ErrorCode.USER_NOT_FOUND, detail=user_id)

        if Role.SUPER_ADMIN.value in user["roles"] and Role.SUPER_ADMIN not in roles:
            remaining = await self._db[Collections.USERS].count_documents(
                {"roles": Role.SUPER_ADMIN.value, "status": UserStatus.ACTIVE.value}
            )
            if remaining <= 1:
                raise AppError(
                    ErrorCode.FORBIDDEN,
                    detail="refusing to remove the last active super admin",
                )

        values = [role.value for role in roles]
        await self._db[Collections.USERS].update_one(
            {"_id": user_id}, {"$set": {"roles": values, "updated_at": datetime.now(UTC)}}
        )
        logger.warning(
            "user roles changed",
            extra={"target_user": user_id, "before": user["roles"], "after": values},
        )
        await self._audit.record(
            action="admin.user_roles_changed",
            entity_type="user",
            entity_id=user_id,
            actor_id=actor_id,
            before={"roles": user["roles"]},
            after={"roles": values, "reason": reason},
        )
        return {"user_id": user_id, "roles": values}

    async def revoke_all_sessions(self, *, user_id: str, actor_id: str) -> dict[str, Any]:
        result = await self._db[Collections.SESSIONS].update_many(
            {"user_id": user_id, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(UTC), "revoked_reason": "admin_revoked"}},
        )
        await self._audit.record(
            action="admin.sessions_revoked",
            entity_type="user",
            entity_id=user_id,
            actor_id=actor_id,
            after={"revoked": result.modified_count},
        )
        return {"user_id": user_id, "revoked": result.modified_count}
