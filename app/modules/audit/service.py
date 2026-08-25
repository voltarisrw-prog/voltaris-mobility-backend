"""Append-only audit trail for financial and administrative actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.logging import request_id_var
from app.infrastructure.database.client import Collections

#: Values scrubbed from before/after snapshots before they are written. An audit
#: record is read by more people than the source document ever is.
SENSITIVE_FIELDS = {"password_hash", "mfa_secret", "api_key", "token", "secret"}


def _scrub(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        key: ("[redacted]" if key in SENSITIVE_FIELDS else value) for key, value in snapshot.items()
    }


class AuditService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex
        await self._db[Collections.AUDIT_LOGS].insert_one(
            {
                "_id": entry_id,
                "actor_id": actor_id,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "before": _scrub(before),
                "after": _scrub(after),
                "request_id": request_id_var.get(),
                "ip": ip,
                "user_agent": user_agent,
                "at": datetime.now(UTC),
            }
        )
        return entry_id
