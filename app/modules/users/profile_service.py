"""The account area: profile, saved vehicles, saved searches, notifications.

Every query is scoped by `user_id` in the filter itself rather than fetched and
then compared. A missing check then produces no rows instead of someone else's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.vehicles.service import VISIBLE, VehicleService

MAX_SAVED_SEARCHES = 25


class ProfileService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db
        self._vehicles = VehicleService(db)

    # -- profile -----------------------------------------------------------

    async def get(self, user_id: str) -> dict[str, Any]:
        row = await self._db[Collections.USERS].find_one({"_id": user_id})
        if row is None:
            raise AppError(ErrorCode.USER_NOT_FOUND, detail=user_id)
        return {
            "id": row["_id"],
            "full_name": row.get("name", ""),
            "email": row["email"],
            "phone": row.get("phone") or "",
            "email_verified": bool(row.get("email_verified")),
            "preferred_language": row.get("preferred_language", "en"),
            "marketing_opt_in": bool(row.get("marketing_opt_in")),
            "created_at": row["created_at"].isoformat(),
        }

    async def update(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        # An allow-list, not the request body. Without it, a `roles` or
        # `email_verified` key in the payload would be written straight through.
        allowed = {"full_name", "phone", "preferred_language", "marketing_opt_in"}
        update = {k: v for k, v in changes.items() if k in allowed and v is not None}
        if "full_name" in update:
            update["name"] = update.pop("full_name")
        if update:
            update["updated_at"] = datetime.now(UTC)
            await self._db[Collections.USERS].update_one({"_id": user_id}, {"$set": update})
        return await self.get(user_id)

    # -- saved vehicles ----------------------------------------------------

    async def saved_vehicles(self, user_id: str) -> dict[str, Any]:
        saves = (
            await self._db[Collections.SAVED_VEHICLES]
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(200)
            .to_list(length=200)
        )
        ids = [row["vehicle_id"] for row in saves]
        if not ids:
            return {"items": [], "page": 1, "per_page": 0, "total": 0, "total_pages": 1}

        rows = await (
            self._db[Collections.VEHICLES]
            .find({"_id": {"$in": ids}, "status": {"$in": VISIBLE}}, {"internal": 0})
            .to_list(length=200)
        )
        by_id = {row["_id"]: row for row in rows}
        # Preserve save order, and silently drop anything since delisted.
        items = [self._vehicles.summary(by_id[i]) for i in ids if i in by_id]
        return {
            "items": items,
            "page": 1,
            "per_page": len(items),
            "total": len(items),
            "total_pages": 1,
        }

    async def save_vehicle(self, user_id: str, vehicle_id: str) -> None:
        exists = await self._db[Collections.VEHICLES].count_documents({"_id": vehicle_id}, limit=1)
        if not exists:
            raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=vehicle_id)
        # An upsert rather than insert-and-catch: idempotent by construction, so a
        # double-tap or a retried request cannot create a second row even where
        # the unique index is not enforced. $setOnInsert keeps the original save
        # time, so re-saving does not silently reorder the list.
        await self._db[Collections.SAVED_VEHICLES].update_one(
            {"user_id": user_id, "vehicle_id": vehicle_id},
            {
                "$setOnInsert": {
                    "_id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "vehicle_id": vehicle_id,
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )

    async def unsave_vehicle(self, user_id: str, vehicle_id: str) -> None:
        await self._db[Collections.SAVED_VEHICLES].delete_one(
            {"user_id": user_id, "vehicle_id": vehicle_id}
        )

    # -- saved searches ----------------------------------------------------

    async def saved_searches(self, user_id: str) -> list[dict[str, Any]]:
        rows = (
            await self._db[Collections.SAVED_SEARCHES]
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(MAX_SAVED_SEARCHES)
            .to_list(length=MAX_SAVED_SEARCHES)
        )
        return [
            {
                "id": row["_id"],
                "label": row["label"],
                "query": row["query"],
                "alerts_enabled": bool(row.get("alerts_enabled", True)),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    async def create_saved_search(self, user_id: str, label: str, query: str) -> dict[str, Any]:
        count = await self._db[Collections.SAVED_SEARCHES].count_documents({"user_id": user_id})
        if count >= MAX_SAVED_SEARCHES:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                detail=f"saved search limit of {MAX_SAVED_SEARCHES} reached",
            )
        row = {
            "_id": uuid.uuid4().hex,
            "user_id": user_id,
            "label": label,
            "query": query,
            "alerts_enabled": True,
            "created_at": datetime.now(UTC),
        }
        await self._db[Collections.SAVED_SEARCHES].insert_one(row)
        return {
            "id": row["_id"],
            "label": label,
            "query": query,
            "alerts_enabled": True,
            "created_at": row["created_at"].isoformat(),
        }

    async def delete_saved_search(self, user_id: str, search_id: str) -> None:
        # user_id in the filter: deleting someone else's search is not possible,
        # and a wrong id simply matches nothing.
        await self._db[Collections.SAVED_SEARCHES].delete_one(
            {"_id": search_id, "user_id": user_id}
        )

    # -- lead history ------------------------------------------------------

    async def inquiries(self, user_id: str) -> dict[str, Any]:
        rows = (
            await self._db[Collections.INQUIRIES]
            .find({"customer_id": user_id})
            .sort("created_at", -1)
            .limit(100)
            .to_list(length=100)
        )
        items = []
        for row in rows:
            vehicle = None
            if row.get("vehicle_id"):
                vehicle = await self._db[Collections.VEHICLES].find_one(
                    {"_id": row["vehicle_id"]},
                    {"slug": 1, "make": 1, "model": 1, "year": 1, "images": 1},
                )
            items.append(
                {
                    "reference": row["reference"],
                    "vehicle": {
                        "id": row.get("vehicle_id") or "",
                        "slug": (vehicle or {}).get("slug", ""),
                        "make": (vehicle or {}).get("make", ""),
                        "model": (vehicle or {}).get("model", ""),
                        "year": (vehicle or {}).get("year", 0),
                        "primary_image": ((vehicle or {}).get("images") or [None])[0],
                    },
                    "status": row["status"].lower(),
                    "created_at": row["created_at"].isoformat(),
                }
            )
        return {
            "items": items,
            "page": 1,
            "per_page": len(items),
            "total": len(items),
            "total_pages": 1,
        }

    async def test_drives(self, user_id: str) -> dict[str, Any]:
        rows = (
            await self._db[Collections.TEST_DRIVES]
            .find({"customer_id": user_id})
            .sort("created_at", -1)
            .limit(100)
            .to_list(length=100)
        )
        items = [
            {
                "reference": row["reference"],
                "vehicle": {
                    "id": row["vehicle_id"],
                    "slug": row.get("vehicle_slug", ""),
                    "make": row.get("vehicle_title", " ").split(" ")[1]
                    if len(row.get("vehicle_title", "").split(" ")) > 1
                    else "",
                    "model": " ".join(row.get("vehicle_title", "").split(" ")[2:]),
                    "year": int(row.get("vehicle_title", "0").split(" ")[0] or 0)
                    if row.get("vehicle_title", "0").split(" ")[0].isdigit()
                    else 0,
                },
                "status": row["status"],
                "scheduled_for": row["scheduled_for"].isoformat()
                if isinstance(row.get("scheduled_for"), datetime)
                else row.get("scheduled_for"),
                "location": row.get("location_slug", ""),
            }
            for row in rows
        ]
        return {
            "items": items,
            "page": 1,
            "per_page": len(items),
            "total": len(items),
            "total_pages": 1,
        }

    async def notifications(self, user_id: str) -> dict[str, Any]:
        rows = (
            await self._db[Collections.NOTIFICATIONS]
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(100)
            .to_list(length=100)
        )
        items = [
            {
                "id": row["_id"],
                "title": row["title"],
                "body": row.get("body", ""),
                "read": bool(row.get("read")),
                "created_at": row["created_at"].isoformat(),
                **({"href": row["href"]} if row.get("href") else {}),
            }
            for row in rows
        ]
        return {
            "items": items,
            "page": 1,
            "per_page": len(items),
            "total": len(items),
            "total_pages": 1,
        }
