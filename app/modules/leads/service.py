"""Enquiries and test drives — the two lead types the marketplace collects."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.audit.service import AuditService

#: A single email may open this many enquiries an hour. The honeypot stops naive
#: bots; this stops the ones that read the form properly.
MAX_INQUIRIES_PER_HOUR = 5


def _reference(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%y%m}-{secrets.token_hex(3).upper()}"


class LeadService:
    def __init__(self, db: AsyncIOMotorDatabase, audit: AuditService) -> None:
        self._db = db
        self._audit = audit

    async def _throttle(self, email: str) -> None:
        recent = await self._db[Collections.INQUIRIES].count_documents(
            {"email": email.lower(), "created_at": {"$gte": datetime.now(UTC) - timedelta(hours=1)}}
        )
        if recent >= MAX_INQUIRIES_PER_HOUR:
            raise AppError(
                ErrorCode.RATE_LIMITED, detail=f"{email} opened {recent} enquiries in an hour"
            )

    async def create_inquiry(
        self,
        *,
        full_name: str,
        email: str,
        phone: str,
        message: str,
        vehicle_id: str | None = None,
        preferred_channel: str | None = None,
        topic: str | None = None,
        source: str = "direct",
        customer_id: str | None = None,
        honeypot: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any]:
        # The honeypot field is invisible and out of the tab order, so a human
        # cannot fill it. Anything non-empty is a bot. Accepted with a normal-looking
        # response rather than rejected, so the operator learns nothing from probing.
        if honeypot:
            await self._audit.record(
                action="inquiry.honeypot_triggered",
                entity_type="inquiry",
                entity_id="rejected",
                after={"email": "[redacted]", "source": source},
                ip=ip,
            )
            return {"reference": _reference("INQ"), "status": "received"}

        await self._throttle(email)

        vehicle_title = None
        if vehicle_id:
            vehicle = await self._db[Collections.VEHICLES].find_one(
                {"_id": vehicle_id}, {"make": 1, "model": 1, "year": 1, "slug": 1, "dealer_id": 1}
            )
            if vehicle is None:
                raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=vehicle_id)
            vehicle_title = f"{vehicle['year']} {vehicle['make']} {vehicle['model']}"

        reference = _reference("INQ")
        await self._db[Collections.INQUIRIES].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "reference": reference,
                "customer_id": customer_id,
                "vehicle_id": vehicle_id,
                "vehicle_title": vehicle_title,
                "full_name": full_name,
                "email": email.lower(),
                "phone": phone,
                "message": message,
                "preferred_channel": preferred_channel,
                # A general enquiry carries a topic instead of a vehicle. Routing
                # differs: no vehicle means the sales queue, not a specific seller.
                "topic": topic,
                "source": source,
                "status": "NEW",
                "assigned_to": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._audit.record(
            action="inquiry.created",
            entity_type="inquiry",
            entity_id=reference,
            actor_id=customer_id,
            after={"vehicle_id": vehicle_id, "topic": topic, "source": source},
            ip=ip,
        )
        return {"reference": reference, "status": "received"}

    async def request_test_drive(
        self,
        *,
        vehicle_id: str,
        full_name: str,
        email: str,
        phone: str,
        preferred_date: str,
        preferred_time_slot: str,
        location_slug: str,
        notes: str | None = None,
        customer_id: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any]:
        vehicle = await self._db[Collections.VEHICLES].find_one(
            {"_id": vehicle_id},
            {"make": 1, "model": 1, "year": 1, "slug": 1, "test_drive_available": 1},
        )
        if vehicle is None:
            raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=vehicle_id)
        if not vehicle.get("test_drive_available", True):
            raise AppError(
                ErrorCode.INVALID_REQUEST, detail="test drives are not offered on this vehicle"
            )

        # A date in the past is a client bug, but accepting it silently means an
        # agent chasing a slot that has already gone.
        try:
            chosen = datetime.fromisoformat(preferred_date).date()
        except ValueError as exc:
            raise AppError(ErrorCode.INVALID_REQUEST, detail=f"bad date {preferred_date}") from exc
        if chosen < datetime.now(UTC).date():
            raise AppError(ErrorCode.INVALID_REQUEST, detail="preferred_date is in the past")

        reference = _reference("TD")
        await self._db[Collections.TEST_DRIVES].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "reference": reference,
                "customer_id": customer_id,
                "vehicle_id": vehicle_id,
                "vehicle_slug": vehicle["slug"],
                "vehicle_title": f"{vehicle['year']} {vehicle['make']} {vehicle['model']}",
                "full_name": full_name,
                "email": email.lower(),
                "phone": phone,
                "preferred_date": preferred_date,
                "preferred_time_slot": preferred_time_slot,
                "location_slug": location_slug,
                "notes": notes,
                # requested, not confirmed. The frontend says as much to the customer;
                # the backend must not imply otherwise.
                "status": "requested",
                "scheduled_for": None,
                "assigned_to": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._audit.record(
            action="test_drive.created",
            entity_type="test_drive",
            entity_id=reference,
            actor_id=customer_id,
            after={"vehicle_id": vehicle_id, "location_slug": location_slug},
            ip=ip,
        )
        return {"reference": reference, "status": "requested", "scheduled_for": None}

    async def get_test_drive(self, reference: str) -> dict[str, Any]:
        """Public status lookup by reference.

        The reference is the only credential, so this returns status and time
        only — never the customer's name, email, or phone. A guessed reference
        then leaks nothing worth having.
        """
        row = await self._db[Collections.TEST_DRIVES].find_one({"reference": reference})
        if row is None:
            raise AppError(ErrorCode.ORDER_NOT_FOUND, detail=reference)
        return {
            "reference": row["reference"],
            "status": row["status"],
            "scheduled_for": row["scheduled_for"].isoformat()
            if isinstance(row.get("scheduled_for"), datetime)
            else row.get("scheduled_for"),
        }
