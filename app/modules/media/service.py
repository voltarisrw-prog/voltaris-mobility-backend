"""Media pipeline: intents in, finished variants out."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.infrastructure.storage.r2 import (
    ALLOWED_CONTENT_TYPES,
    MAX_UPLOAD_BYTES,
    PUBLIC_PREFIX,
    QUARANTINE_PREFIX,
    R2Storage,
)
from app.modules.audit.service import AuditService
from app.modules.media.processor import VARIANTS, render_variants

logger = logging.getLogger("voltaris.media")

MAX_PHOTOS_PER_LISTING = 12


class MediaService:
    def __init__(self, db: AsyncIOMotorDatabase, storage: R2Storage, audit: AuditService) -> None:
        self._db = db
        self._storage = storage
        self._audit = audit

    async def create_intents(
        self,
        *,
        owner_id: str | None,
        files: list[dict[str, Any]],
        listing_reference: str | None = None,
    ) -> list[dict[str, Any]]:
        if not files:
            raise AppError(ErrorCode.INVALID_REQUEST, detail="no files requested")
        if len(files) > MAX_PHOTOS_PER_LISTING:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                detail=f"{len(files)} files exceeds the {MAX_PHOTOS_PER_LISTING} limit",
            )

        intents = []
        for entry in files:
            intent = self._storage.create_upload_intent(
                content_type=entry["content_type"], size_bytes=entry["size_bytes"]
            )
            # Record the intent before handing out the URL. An object in the bucket
            # with no matching row is an orphan the lifecycle rule will reap; a row
            # with no object is simply never finalised. Both fail safe.
            await self._db[Collections.MEDIA].insert_one(
                {
                    "_id": uuid.uuid4().hex,
                    "media_key": intent.media_key,
                    "owner_id": owner_id,
                    "listing_reference": listing_reference,
                    "declared_content_type": entry["content_type"],
                    "declared_size": entry["size_bytes"],
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                }
            )
            intents.append(
                {
                    "upload_url": intent.upload_url,
                    "media_key": intent.media_key,
                    "expires_at": intent.expires_at,
                }
            )
        return intents

    async def finalize(
        self, *, media_keys: list[str], owner_id: str | None, alt_prefix: str
    ) -> list[dict[str, Any]]:
        """Download each quarantined original, re-encode it, publish the variants.

        Runs inline. At this volume a handful of 12 MB decodes on the request path
        is acceptable and keeps the failure visible to the person waiting. It is
        the first thing to move to a queue when listings arrive faster than a
        reviewer can look at them — the interface does not change, only the caller.
        """
        images: list[dict[str, Any]] = []

        for index, media_key in enumerate(media_keys):
            record = await self._db[Collections.MEDIA].find_one({"media_key": media_key})
            if record is None:
                raise AppError(ErrorCode.INVALID_REQUEST, detail=f"unknown media key {media_key}")
            # Ownership check: a media key is a bearer reference, so without this
            # anyone could attach someone else's upload to their own listing.
            if owner_id and record.get("owner_id") and record["owner_id"] != owner_id:
                raise AppError(ErrorCode.FORBIDDEN, detail=f"{media_key} belongs to another user")
            if record["status"] == "published":
                images.append(record["image"])
                continue
            if not media_key.startswith(f"{QUARANTINE_PREFIX}/"):
                raise AppError(ErrorCode.INVALID_REQUEST, detail="key is not in quarantine")

            original = self._storage.download(media_key)
            if len(original) > MAX_UPLOAD_BYTES:
                await self._reject(media_key, "larger than the declared size")
                raise AppError(ErrorCode.INVALID_REQUEST, detail="uploaded file exceeds the limit")

            try:
                processed = render_variants(original, alt=f"{alt_prefix} — photo {index + 1}")
            except AppError:
                await self._reject(media_key, "failed validation or decoding")
                raise

            folder = f"{PUBLIC_PREFIX}/{datetime.now(UTC):%Y/%m}/{secrets.token_urlsafe(16)}"
            for name in VARIANTS:
                url = self._storage.put_public(
                    key=f"{folder}/{name}.webp",
                    body=processed._rendered[name],
                    content_type="image/webp",
                )
                setattr(processed, name, url)

            document = processed.as_document()
            await self._db[Collections.MEDIA].update_one(
                {"media_key": media_key},
                {
                    "$set": {
                        "status": "published",
                        "image": document,
                        "published_at": datetime.now(UTC),
                    }
                },
            )

            # The original is the only copy carrying EXIF. Once variants exist it
            # has no further use, and keeping it means keeping the GPS coordinates.
            self._storage.delete(media_key)
            images.append(document)

        await self._audit.record(
            action="media.published",
            entity_type="media",
            entity_id=alt_prefix[:64],
            actor_id=owner_id,
            after={"count": len(images)},
        )
        return images

    async def _reject(self, media_key: str, reason: str) -> None:
        logger.warning("rejected upload", extra={"media_key": media_key, "reason": reason})
        await self._db[Collections.MEDIA].update_one(
            {"media_key": media_key},
            {"$set": {"status": "rejected", "reason": reason, "rejected_at": datetime.now(UTC)}},
        )
        self._storage.delete(media_key)


_ = ALLOWED_CONTENT_TYPES
