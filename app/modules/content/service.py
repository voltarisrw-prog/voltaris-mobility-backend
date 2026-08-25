"""Editorial: guides and blog posts.

One store, one endpoint, split by `kind`. Guides are evergreen and edited in
place; posts are dated and immutable once published. They share a shape but never
a listing query, which is why `kind` leads the compound index.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.infrastructure.database.client import Collections

PUBLISHED = "published"
KINDS = ("guide", "blog")


class ContentService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def list(
        self,
        *,
        kind: str | None = None,
        category: str | None = None,
        page: int = 1,
        per_page: int = 24,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"status": PUBLISHED}
        if kind:
            if kind not in KINDS:
                raise AppError(ErrorCode.INVALID_REQUEST, detail=f"unknown kind {kind}")
            query["kind"] = kind
        if category:
            query["category"] = category

        rows = (
            await self._db[Collections.ARTICLES]
            .find(query, {"body_html": 0})
            .sort("published_at", -1)
            .skip((page - 1) * per_page)
            .limit(per_page)
            .to_list(length=per_page)
        )
        total = await self._db[Collections.ARTICLES].count_documents(query)
        return {
            "items": [self.summary(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
        }

    async def get(self, slug: str) -> dict[str, Any]:
        row = await self._db[Collections.ARTICLES].find_one({"slug": slug, "status": PUBLISHED})
        if row is None:
            raise AppError(ErrorCode.VEHICLE_NOT_FOUND, detail=f"article {slug}")
        return {
            **self.summary(row),
            "body_html": row.get("body_html", ""),
            "faqs": row.get("faqs") or [],
            "related_slugs": row.get("related_slugs") or [],
        }

    async def sitemap(self, kind: str | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"status": PUBLISHED}
        if kind:
            query["kind"] = kind
        rows = (
            await self._db[Collections.ARTICLES]
            .find(query, {"slug": 1, "updated_at": 1})
            .sort("updated_at", -1)
            .limit(5000)
            .to_list(length=5000)
        )
        return {
            "items": [
                {"slug": row["slug"], "updated_at": row["updated_at"].isoformat()} for row in rows
            ],
            "page": 1,
            "per_page": 5000,
            "total": len(rows),
            "total_pages": 1,
        }

    @staticmethod
    def summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "slug": row["slug"],
            "kind": row.get("kind", "guide"),
            "title": row["title"],
            "excerpt": row.get("excerpt", ""),
            "category": row.get("category", "insights"),
            "cover_image": row.get("cover_image"),
            "author": row.get("author", "Voltaris"),
            "reading_minutes": row.get("reading_minutes", 3),
            "published_at": row["published_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
