"""Public charging directory."""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.infrastructure.database.client import Collections


class ChargingService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def list(self, district: str | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"published": True}
        if district:
            query["district"] = district

        rows = (
            await self._db[Collections.CHARGING_LOCATIONS]
            .find(query)
            .sort([("district", 1), ("name", 1)])
            .limit(500)
            .to_list(length=500)
        )
        return {
            "items": [
                {
                    "id": row["_id"],
                    "slug": row["slug"],
                    "name": row["name"],
                    "operator": row.get("operator", ""),
                    "district": row.get("district", ""),
                    "address": row.get("address", ""),
                    # GeoJSON is [lng, lat]; the frontend wants them named and
                    # the other way round. Getting this backwards puts Kigali
                    # in the Indian Ocean.
                    "latitude": (row.get("location") or {}).get("coordinates", [0, 0])[1],
                    "longitude": (row.get("location") or {}).get("coordinates", [0, 0])[0],
                    "connectors": row.get("connectors") or [],
                    "access": row.get("access", "public"),
                    "open_hours": row.get("open_hours", ""),
                    "verified_at": row["verified_at"].isoformat()
                    if row.get("verified_at")
                    else None,
                }
                for row in rows
            ],
            "page": 1,
            "per_page": len(rows),
            "total": len(rows),
            "total_pages": 1,
        }
