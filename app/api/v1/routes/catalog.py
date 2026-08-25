"""Dealers, editorial content, and the charging directory. All public reads."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import ChargingServiceDep, ContentServiceDep, DealerServiceDep

router = APIRouter(tags=["catalog"])


@router.get("/dealers")
async def list_dealers(
    service: DealerServiceDep, page: Annotated[int, Query(ge=1)] = 1
) -> dict[str, Any]:
    return await service.list(page=page)


@router.get("/dealers/{slug}")
async def get_dealer(slug: str, service: DealerServiceDep) -> dict[str, Any]:
    return await service.get(slug)


@router.get("/dealers/{slug}/vehicles")
async def dealer_vehicles(
    slug: str, service: DealerServiceDep, page: Annotated[int, Query(ge=1)] = 1
) -> dict[str, Any]:
    return await service.vehicles(slug, page=page)


@router.get("/content/articles")
async def list_articles(
    service: ContentServiceDep,
    kind: str | None = None,
    category: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict[str, Any]:
    return await service.list(kind=kind, category=category, page=page)


@router.get("/content/sitemap")
async def content_sitemap(service: ContentServiceDep, kind: str | None = None) -> dict[str, Any]:
    return await service.sitemap(kind=kind)


# After /content/sitemap, so the literal path is not captured by the slug.
@router.get("/content/articles/{slug}")
async def get_article(slug: str, service: ContentServiceDep) -> dict[str, Any]:
    return await service.get(slug)


@router.get("/charging/locations")
async def charging_locations(
    service: ChargingServiceDep, district: str | None = None
) -> dict[str, Any]:
    return await service.list(district=district)
