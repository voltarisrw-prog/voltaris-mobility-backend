"""Public vehicle queries.

Two rules govern this module.

**Nothing internal escapes.** Every read applies `PUBLIC_PROJECTION` (`{"internal": 0}`),
so seller expectations, commission rates, and internal notes cannot reach a public
response even if a serialiser is later edited carelessly. The projection is applied
in the repository call, not in the serialiser, because that is the layer no future
change can accidentally bypass.

**The wire format is the frontend's, not the database's.** Stored status is an
uppercase lifecycle enum with seven states, four of which the public must never see.
The API exposes four lowercase values. Translating here rather than leaking the
lifecycle keeps `DRAFT` and `PENDING_REVIEW` from ever appearing in a payload.
"""

from __future__ import annotations

import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections
from app.modules.vehicles.models import PUBLIC_PROJECTION, VehicleStatus

#: Stored lifecycle -> the four values the frontend's `ListingStatus` union allows.
PUBLIC_STATUS = {
    VehicleStatus.AVAILABLE.value: "available",
    VehicleStatus.RESERVED.value: "reserved",
    VehicleStatus.SOLD.value: "sold",
    VehicleStatus.UNPUBLISHED.value: "unavailable",
}

#: Only these are ever visible publicly. DRAFT, PENDING_REVIEW and REJECTED are
#: absent by construction rather than by a filter someone might forget.
VISIBLE = list(PUBLIC_STATUS.keys())

SORTS: dict[str, list[tuple[str, int]]] = {
    "relevance": [("verified", -1), ("published_at", -1)],
    "newest": [("published_at", -1)],
    "price_asc": [("agency_price", 1)],
    "price_desc": [("agency_price", -1)],
    "range_desc": [("range_km", -1)],
    "mileage_asc": [("mileage_km", 1)],
    "year_desc": [("year", -1)],
}

MAX_PER_PAGE = 48


def _csv(value: str | None) -> list[str]:
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


class VehicleService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    # -- query building ----------------------------------------------------

    def _filter(self, params: dict[str, Any]) -> dict[str, Any]:
        query: dict[str, Any] = {"status": {"$in": VISIBLE}, "deleted_at": None}

        makes = _csv(params.get("make"))
        if makes:
            query["make_slug"] = {"$in": makes}
        bodies = _csv(params.get("body"))
        if bodies:
            query["body_type"] = {"$in": bodies}
        if params.get("model"):
            # Anchored and escaped: an unescaped user string here is a regex
            # injection, and an unanchored one cannot use the index.
            query["model"] = {"$regex": f"^{re.escape(str(params['model']))}", "$options": "i"}
        if params.get("condition"):
            query["condition"] = params["condition"]
        if params.get("location"):
            query["location.slug"] = str(params["location"]).lower()
        if params.get("verified") is True:
            query["verified"] = True
        if params.get("dealer_id"):
            query["dealer_id"] = params["dealer_id"]

        mode = params.get("mode")
        if mode == "rental":
            query["rental_enabled"] = True
        elif mode == "sale":
            query["purchase_enabled"] = True

        def between(field: str, low: str, high: str) -> None:
            bounds: dict[str, Any] = {}
            if params.get(low) is not None:
                bounds["$gte"] = params[low]
            if params.get(high) is not None:
                bounds["$lte"] = params[high]
            if bounds:
                query[field] = bounds

        between("agency_price", "minPrice", "maxPrice")
        between("year", "minYear", "maxYear")

        if params.get("maxMileage") is not None:
            query["mileage_km"] = {"$lte": params["maxMileage"]}
        if params.get("minRange") is not None:
            query["range_km"] = {"$gte": params["minRange"]}
        if params.get("minBattery") is not None:
            query["battery_kwh"] = {"$gte": params["minBattery"]}

        if params.get("q"):
            query["$text"] = {"$search": str(params["q"])}

        return query

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        page = max(1, int(params.get("page") or 1))
        per_page = min(MAX_PER_PAGE, max(1, int(params.get("per_page") or 24)))
        query = self._filter(params)

        sort = SORTS.get(str(params.get("sort") or "relevance"), SORTS["relevance"])
        # A text search sorts by textScore first; anything else ignores relevance
        # entirely and returns effectively arbitrary matches.
        projection = dict(PUBLIC_PROJECTION)
        if "$text" in query and params.get("sort") in (None, "relevance"):
            projection["score"] = {"$meta": "textScore"}
            sort = [("score", {"$meta": "textScore"})]  # type: ignore[list-item]

        cursor = (
            self._db[Collections.VEHICLES]
            .find(query, projection)
            .sort(sort)
            .skip((page - 1) * per_page)
            .limit(per_page)
        )
        rows = await cursor.to_list(length=per_page)

        # Capped: an exact count over tens of millions of documents is a table scan
        # that nobody reading page 1 of a marketplace is waiting for.
        total = await self._db[Collections.VEHICLES].count_documents(query, limit=10_000)

        return {
            "items": [self.summary(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
        }

    async def get_by_slug(self, slug: str) -> dict[str, Any]:
        row = await self._db[Collections.VEHICLES].find_one(
            {"slug": slug, "status": {"$in": VISIBLE}, "deleted_at": None},
            PUBLIC_PROJECTION,
        )
        if row is None:
            raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=slug)
        return await self.detail(row)

    async def similar(self, vehicle_id: str, limit: int = 6) -> list[dict[str, Any]]:
        base = await self._db[Collections.VEHICLES].find_one({"_id": vehicle_id}, PUBLIC_PROJECTION)
        if base is None:
            return []

        # Same body type, comparable price, never itself. Ordered by how close the
        # price is, so "similar" means something rather than "also a car".
        price = base.get("agency_price") or 0
        rows = (
            await self._db[Collections.VEHICLES]
            .find(
                {
                    "_id": {"$ne": vehicle_id},
                    "status": {"$in": VISIBLE},
                    "deleted_at": None,
                    "body_type": base.get("body_type"),
                    **(
                        {"agency_price": {"$gte": int(price * 0.7), "$lte": int(price * 1.3)}}
                        if price
                        else {}
                    ),
                },
                PUBLIC_PROJECTION,
            )
            .limit(limit * 2)
            .to_list(length=limit * 2)
        )
        rows.sort(key=lambda row: abs((row.get("agency_price") or 0) - price))
        return [self.summary(row) for row in rows[:limit]]

    async def compare(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids or len(ids) > 4:
            raise AppError(
                ErrorCode.INVALID_REQUEST, detail=f"compare accepts 1-4 ids, got {len(ids)}"
            )
        rows = await (
            self._db[Collections.VEHICLES]
            .find({"_id": {"$in": ids}, "status": {"$in": VISIBLE}}, PUBLIC_PROJECTION)
            .to_list(length=4)
        )
        by_id = {row["_id"]: row for row in rows}
        # Preserve the caller's order — the comparison columns must match the URL.
        return [await self.detail(by_id[i]) for i in ids if i in by_id]

    async def facets(self) -> dict[str, Any]:
        base = {"status": {"$in": VISIBLE}, "deleted_at": None}

        async def group(field: str, label_field: str | None = None) -> list[dict[str, Any]]:
            pipeline = [
                {"$match": base},
                {
                    "$group": {
                        "_id": f"${field}",
                        "count": {"$sum": 1},
                        "label": {"$first": f"${label_field or field}"},
                    }
                },
                {"$sort": {"count": -1}},
                {"$limit": 40},
            ]
            rows = await self._db[Collections.VEHICLES].aggregate(pipeline).to_list(length=40)
            return [
                {
                    "value": str(row["_id"]),
                    "label": str(row.get("label") or row["_id"]).title(),
                    "count": row["count"],
                }
                for row in rows
                if row["_id"]
            ]

        bounds = (
            await self._db[Collections.VEHICLES]
            .aggregate(
                [
                    {"$match": base},
                    {
                        "$group": {
                            "_id": None,
                            "minPrice": {"$min": "$agency_price"},
                            "maxPrice": {"$max": "$agency_price"},
                            "minRange": {"$min": "$range_km"},
                            "maxRange": {"$max": "$range_km"},
                        }
                    },
                ]
            )
            .to_list(length=1)
        )
        b = bounds[0] if bounds else {}

        return {
            "makes": await group("make_slug", "make"),
            "bodies": await group("body_type"),
            "locations": await group("location.slug", "location.city"),
            "price": {"min": b.get("minPrice") or 0, "max": b.get("maxPrice") or 0},
            "range": {"min": b.get("minRange") or 0, "max": b.get("maxRange") or 0},
        }

    async def sitemap(self, page: int = 1, per_page: int = 5000) -> dict[str, Any]:
        rows = (
            await self._db[Collections.VEHICLES]
            .find(
                {"status": {"$in": VISIBLE}, "deleted_at": None},
                {"slug": 1, "updated_at": 1, "status": 1},
            )
            .sort("updated_at", -1)
            .skip((page - 1) * per_page)
            .limit(per_page)
            .to_list(length=per_page)
        )
        total = await self._db[Collections.VEHICLES].count_documents(
            {"status": {"$in": VISIBLE}, "deleted_at": None}
        )
        return {
            "items": [
                {
                    "slug": row["slug"],
                    "updated_at": row["updated_at"].isoformat(),
                    "status": PUBLIC_STATUS.get(row["status"], "unavailable"),
                }
                for row in rows
            ],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
        }

    # -- serialisation -----------------------------------------------------

    @staticmethod
    def summary(row: dict[str, Any]) -> dict[str, Any]:
        images = row.get("images") or []
        return {
            "id": row["_id"],
            "slug": row["slug"],
            "make": row["make"],
            "model": row["model"],
            "variant": row.get("variant"),
            "year": row["year"],
            "price": row.get("agency_price"),
            "currency": row.get("currency", "RWF"),
            "rental_price_per_day": row.get("rental_price_per_day"),
            "mileage_km": row["mileage_km"],
            "battery_kwh": row["battery_kwh"],
            "range_km": row["range_km"],
            "body_type": row["body_type"],
            "condition": row["condition"],
            "location": row["location"],
            "listing_mode": (
                "sale_and_rental"
                if row.get("purchase_enabled") and row.get("rental_enabled")
                else "rental"
                if row.get("rental_enabled")
                else "sale"
            ),
            "status": PUBLIC_STATUS.get(row["status"], "unavailable"),
            "verified": bool(row.get("verified")),
            "primary_image": images[0] if images else None,
            "published_at": (row.get("published_at") or row["created_at"]).isoformat(),
        }

    async def detail(self, row: dict[str, Any]) -> dict[str, Any]:
        seller = await self._seller(row)
        return {
            **self.summary(row),
            "description": row.get("description", ""),
            "images": row.get("images") or [],
            "seller": seller,
            "drivetrain": row.get("drivetrain", "fwd"),
            "power_kw": row.get("power_kw", 0),
            "torque_nm": row.get("torque_nm"),
            "top_speed_kph": row.get("top_speed_kph"),
            "seats": row.get("seats", 5),
            "doors": row.get("doors", 5),
            "charging": row.get("charging")
            or {"ac_kw": 0, "dc_kw": None, "port_type": "unknown", "dc_10_80_minutes": None},
            "dimensions": row.get("dimensions"),
            "warranty": row.get("warranty"),
            "features": row.get("features") or [],
            "financing_available": bool(row.get("financing_available")),
            "test_drive_available": bool(row.get("test_drive_available", True)),
            "purchase_enabled": bool(row.get("purchase_enabled")),
            "rental_enabled": bool(row.get("rental_enabled")),
            "faqs": row.get("faqs") or [],
            "updated_at": row["updated_at"].isoformat(),
        }

    async def _seller(self, row: dict[str, Any]) -> dict[str, Any]:
        """Seller identity for a public page.

        Phone and WhatsApp are omitted unless the dealer has opted into public
        display. Private sellers never get their number published — that is what
        the enquiry flow is for, and publishing it would make the marketplace a
        scraper's contact list.
        """
        if row.get("dealer_id"):
            dealer = await self._db[Collections.DEALERS].find_one({"_id": row["dealer_id"]})
            if dealer:
                contact = (
                    {"phone": dealer.get("phone"), "whatsapp": dealer.get("whatsapp")}
                    if dealer.get("public_contact")
                    else {}
                )
                return {
                    "type": "dealer",
                    "display_name": dealer["name"],
                    "slug": dealer["slug"],
                    "verified": bool(dealer.get("verified")),
                    **{k: v for k, v in contact.items() if v},
                }
        return {
            "type": "private",
            "display_name": row.get("seller_display_name") or "Private owner",
            "verified": bool(row.get("verified")),
        }
