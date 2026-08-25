"""Order creation and state transitions.

Three properties this file exists to guarantee:

1. **The price is ours.** The client sends a vehicle id and an intent. Nothing else
   about money is read from the request, so there is no parameter to tamper with.
2. **One order per vehicle.** Enforced by a unique partial index, not by a read-then-
   write check, so two simultaneous checkouts cannot both pass the check.
3. **Retries are free.** An `Idempotency-Key` replays the original result instead of
   creating a second order.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.audit.service import AuditService
from app.modules.orders.models import (
    ACTIVE_ORDER_STATUSES,
    OrderKind,
    OrderStatus,
    can_transition,
)
from app.modules.vehicles.models import VehicleStatus

IDEMPOTENCY_TTL = timedelta(hours=24)


def _reference() -> str:
    return f"VM-{datetime.now(UTC):%Y%m}-{secrets.token_hex(3).upper()}"


class OrderService:
    def __init__(self, db: AsyncIOMotorDatabase, audit: AuditService) -> None:
        self._db = db
        self._audit = audit
        self._settings = get_settings()

    # -- idempotency -------------------------------------------------------

    async def _replay_or_reserve(
        self, *, user_id: str, endpoint: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return a stored response for a repeated key, or claim the key.

        The request body is fingerprinted alongside the key. Reusing a key with a
        different body is a client bug that would otherwise return someone else's
        order, so it is rejected rather than replayed.
        """
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

        existing = await self._db[Collections.IDEMPOTENCY].find_one(
            {"user_id": user_id, "endpoint": endpoint, "key": key}
        )
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise AppError(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    detail=f"key {key} previously used with a different body",
                )
            if existing.get("response") is None:
                # A concurrent request holds the key and has not finished. Telling the
                # client to retry is safer than racing it.
                raise AppError(ErrorCode.DUPLICATE_REQUEST, detail="request in flight")
            return existing["response"]

        try:
            await self._db[Collections.IDEMPOTENCY].insert_one(
                {
                    "_id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "endpoint": endpoint,
                    "key": key,
                    "fingerprint": fingerprint,
                    "response": None,
                    "created_at": datetime.now(UTC),
                    "expires_at": datetime.now(UTC) + IDEMPOTENCY_TTL,
                }
            )
        except DuplicateKeyError as exc:
            # Lost the race to claim the key; the winner is still working.
            raise AppError(
                ErrorCode.DUPLICATE_REQUEST, detail="concurrent same-key request"
            ) from exc
        return None

    async def _store_idempotent_response(
        self, *, user_id: str, endpoint: str, key: str, response: dict[str, Any]
    ) -> None:
        await self._db[Collections.IDEMPOTENCY].update_one(
            {"user_id": user_id, "endpoint": endpoint, "key": key},
            {"$set": {"response": response, "completed_at": datetime.now(UTC)}},
        )

    # -- creation ----------------------------------------------------------

    async def create_order(
        self,
        *,
        customer_id: str,
        vehicle_id: str,
        kind: OrderKind = OrderKind.PURCHASE,
        rental_days: int | None = None,
        idempotency_key: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any]:
        request_payload = {
            "vehicle_id": vehicle_id,
            "kind": kind.value,
            "rental_days": rental_days,
        }

        if idempotency_key:
            replay = await self._replay_or_reserve(
                user_id=customer_id,
                endpoint="POST /orders",
                key=idempotency_key,
                payload=request_payload,
            )
            if replay is not None:
                return replay

        vehicle = await self._db[Collections.VEHICLES].find_one({"_id": vehicle_id})
        if vehicle is None or vehicle.get("deleted_at") is not None:
            raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=f"vehicle {vehicle_id}")

        if vehicle["status"] != VehicleStatus.AVAILABLE.value:
            raise AppError(
                ErrorCode.VEHICLE_UNAVAILABLE,
                detail=f"vehicle {vehicle_id} is {vehicle['status']}",
            )

        if kind is OrderKind.PURCHASE and not vehicle.get("purchase_enabled"):
            raise AppError(ErrorCode.VEHICLE_NOT_PURCHASABLE, detail=f"vehicle {vehicle_id}")
        if kind is OrderKind.RENTAL and not vehicle.get("rental_enabled"):
            raise AppError(ErrorCode.VEHICLE_NOT_PURCHASABLE, detail="rental not enabled")

        total, lines = self._price(vehicle, kind, rental_days)

        order_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        document = {
            "_id": order_id,
            "reference": _reference(),
            "customer_id": customer_id,
            "vehicle_id": vehicle_id,
            "vehicle_slug": vehicle["slug"],
            "vehicle_title": f"{vehicle['year']} {vehicle['make']} {vehicle['model']}",
            "kind": kind.value,
            "lines": lines,
            "total": total,
            "currency": vehicle.get("currency", self._settings.default_currency),
            "status": OrderStatus.PENDING.value,
            "payment_id": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }

        try:
            await self._db[Collections.ORDERS].insert_one(document)
        except DuplicateKeyError as exc:
            # The partial unique index on vehicle_id rejected this because another
            # order already holds the vehicle. This is the concurrency guard firing —
            # the loser of the race learns it lost here, not from a stale read.
            raise AppError(
                ErrorCode.VEHICLE_UNAVAILABLE,
                detail=f"vehicle {vehicle_id} already has an active order",
            ) from exc

        # Reserve the vehicle. Conditional on it still being AVAILABLE, so a status
        # change between the read above and here cannot be overwritten.
        reserved = await self._db[Collections.VEHICLES].update_one(
            {"_id": vehicle_id, "status": VehicleStatus.AVAILABLE.value},
            {
                "$set": {"status": VehicleStatus.RESERVED.value, "updated_at": now},
                "$inc": {"version": 1},
            },
        )
        if reserved.modified_count != 1:
            # Someone else changed the vehicle first. Roll the order back rather than
            # leaving an order pointing at a vehicle we do not hold.
            await self._db[Collections.ORDERS].delete_one({"_id": order_id})
            raise AppError(
                ErrorCode.VEHICLE_UNAVAILABLE,
                detail=f"vehicle {vehicle_id} was taken during checkout",
            )

        await self._audit.record(
            action="order.created",
            entity_type="order",
            entity_id=order_id,
            actor_id=customer_id,
            after={"status": OrderStatus.PENDING.value, "total": total, "vehicle_id": vehicle_id},
            ip=ip,
        )

        response = self._serialise(document)
        if idempotency_key:
            await self._store_idempotent_response(
                user_id=customer_id,
                endpoint="POST /orders",
                key=idempotency_key,
                response=response,
            )
        return response

    def _price(
        self, vehicle: dict[str, Any], kind: OrderKind, rental_days: int | None
    ) -> tuple[int, list[dict[str, Any]]]:
        """Authoritative pricing. Reads only the vehicle document."""
        currency = vehicle.get("currency", self._settings.default_currency)

        if kind is OrderKind.RENTAL:
            daily = vehicle.get("rental_price_per_day")
            if not daily:
                raise AppError(ErrorCode.VEHICLE_NOT_PURCHASABLE, detail="no rental price set")
            days = rental_days or 1
            if days < 1 or days > 365:
                raise AppError(ErrorCode.INVALID_REQUEST, detail=f"rental_days={days}")
            total = daily * days
            return total, [{"label": f"Rental, {days} day(s)", "amount": total}]

        price = vehicle.get("agency_price")
        if not price:
            # A listing with no public price cannot be transacted, whatever the
            # seller's internal expectation is.
            raise AppError(ErrorCode.VEHICLE_NOT_PURCHASABLE, detail="no agency price set")

        lines = [{"label": "Vehicle", "amount": price}]
        _ = currency
        return price, lines

    # -- transitions -------------------------------------------------------

    async def transition(
        self,
        *,
        order_id: str,
        target: OrderStatus,
        actor_id: str | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        order = await self._db[Collections.ORDERS].find_one({"_id": order_id})
        if order is None:
            raise AppError(ErrorCode.ORDER_NOT_FOUND, detail=order_id)

        current = OrderStatus(order["status"])
        if not can_transition(current, target):
            raise AppError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"order {order_id}: {current} -> {target}",
            )

        # Compare-and-set on both status and version: two concurrent transitions
        # cannot both succeed, and the loser gets a clean conflict.
        updated = await self._db[Collections.ORDERS].find_one_and_update(
            {"_id": order_id, "status": current.value, "version": order["version"]},
            {
                "$set": {"status": target.value, "updated_at": datetime.now(UTC)},
                "$inc": {"version": 1},
            },
            return_document=True,
        )
        if updated is None:
            raise AppError(
                ErrorCode.CONCURRENT_MODIFICATION,
                detail=f"order {order_id} changed during transition",
            )

        await self._release_or_sell(order, target)

        await self._audit.record(
            action="order.status_changed",
            entity_type="order",
            entity_id=order_id,
            actor_id=actor_id,
            before={"status": current.value},
            after={"status": target.value, "reason": reason},
        )
        return self._serialise(updated)

    async def _release_or_sell(self, order: dict[str, Any], target: OrderStatus) -> None:
        """Keep the vehicle's status in step with the order that holds it."""
        if target is OrderStatus.CANCELLED:
            await self._db[Collections.VEHICLES].update_one(
                {"_id": order["vehicle_id"], "status": VehicleStatus.RESERVED.value},
                {
                    "$set": {
                        "status": VehicleStatus.AVAILABLE.value,
                        "updated_at": datetime.now(UTC),
                    },
                    "$inc": {"version": 1},
                },
            )
        elif target is OrderStatus.PAID:
            await self._db[Collections.VEHICLES].update_one(
                {"_id": order["vehicle_id"]},
                {
                    "$set": {"status": VehicleStatus.SOLD.value, "updated_at": datetime.now(UTC)},
                    "$inc": {"version": 1},
                },
            )

    # -- reads -------------------------------------------------------------

    async def get_for_customer(self, *, order_id: str, customer_id: str) -> dict[str, Any]:
        """Object-level authorization: the owner filter is in the query itself.

        Fetching then comparing invites the check to be forgotten in a later edit;
        this way an order belonging to someone else is simply not found.
        """
        order = await self._db[Collections.ORDERS].find_one(
            {"_id": order_id, "customer_id": customer_id}
        )
        if order is None:
            raise AppError(ErrorCode.ORDER_NOT_FOUND, detail=order_id)
        return self._serialise(order)

    async def list_for_customer(
        self, *, customer_id: str, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        query: dict[str, Any] = {"customer_id": customer_id}
        if cursor:
            # Cursor pagination on an indexed key. `skip` degrades linearly and is
            # unusable past a few thousand documents.
            query["created_at"] = {"$lt": datetime.fromisoformat(cursor)}
        rows = (
            await self._db[Collections.ORDERS]
            .find(query)
            .sort("created_at", -1)
            .limit(limit + 1)
            .to_list(length=limit + 1)
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["created_at"].isoformat() if has_more and rows else None
        return [self._serialise(row) for row in rows], next_cursor

    @staticmethod
    def _serialise(document: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": document["_id"],
            "reference": document["reference"],
            "vehicle": {
                "id": document["vehicle_id"],
                "slug": document["vehicle_slug"],
                "title": document["vehicle_title"],
            },
            "kind": document["kind"],
            "status": document["status"],
            "lines": document["lines"],
            "total": document["total"],
            "currency": document["currency"],
            "created_at": document["created_at"].isoformat(),
        }

    @staticmethod
    def active_statuses() -> tuple[str, ...]:
        return tuple(status.value for status in ACTIVE_ORDER_STATUSES)
