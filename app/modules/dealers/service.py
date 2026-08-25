"""Dealer directory and profiles."""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.vehicles.service import VISIBLE, VehicleService


class DealerService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db
        self._vehicles = VehicleService(db)

    async def list(self, page: int = 1, per_page: int = 24) -> dict[str, Any]:
        query = {"status": "active"}
        rows = (
            await self._db[Collections.DEALERS]
            .find(query)
            .sort([("verified", -1), ("name", 1)])
            .skip((page - 1) * per_page)
            .limit(per_page)
            .to_list(length=per_page)
        )
        total = await self._db[Collections.DEALERS].count_documents(query)
        return {
            "items": [await self.summary(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
        }

    async def get(self, slug: str) -> dict[str, Any]:
        row = await self._db[Collections.DEALERS].find_one({"slug": slug, "status": "active"})
        if row is None:
            raise AppError(ErrorCode.DEALER_NOT_FOUND, detail=slug)
        return {
            **(await self.summary(row)),
            "description": row.get("description", ""),
            "address": row.get("address", ""),
            # Contact details only when the dealer has opted in. Otherwise the
            # directory becomes a scraper's phone list.
            **(
                {"phone": row.get("phone")}
                if row.get("public_contact") and row.get("phone")
                else {}
            ),
            **(
                {"whatsapp": row.get("whatsapp")}
                if row.get("public_contact") and row.get("whatsapp")
                else {}
            ),
            **({"website": row.get("website")} if row.get("website") else {}),
            **(
                {"established_year": row["established_year"]} if row.get("established_year") else {}
            ),
            "cover_image_url": row.get("cover_image_url"),
        }

    async def vehicles(self, slug: str, page: int = 1, per_page: int = 24) -> dict[str, Any]:
        dealer = await self._db[Collections.DEALERS].find_one({"slug": slug}, {"_id": 1})
        if dealer is None:
            raise AppError(ErrorCode.DEALER_NOT_FOUND, detail=slug)
        return await self._vehicles.list(
            {"page": page, "per_page": per_page, "dealer_id": dealer["_id"]}
        )

    async def summary(self, row: dict[str, Any]) -> dict[str, Any]:
        count = await self._db[Collections.VEHICLES].count_documents(
            {"dealer_id": row["_id"], "status": {"$in": VISIBLE}, "deleted_at": None}
        )
        return {
            "id": row["_id"],
            "slug": row["slug"],
            "name": row["name"],
            "verified": bool(row.get("verified")),
            "city": row.get("city", ""),
            "logo_url": row.get("logo_url"),
            "vehicle_count": count,
        }
