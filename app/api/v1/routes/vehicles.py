"""Public vehicle endpoints. No authentication: this is the marketplace."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import VehicleServiceDep

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


@router.get("")
async def list_vehicles(
    service: VehicleServiceDep,
    q: str | None = None,
    make: str | None = None,
    model: str | None = None,
    body: str | None = None,
    condition: str | None = None,
    location: str | None = None,
    mode: str | None = None,
    minPrice: int | None = None,
    maxPrice: int | None = None,
    minYear: int | None = None,
    maxYear: int | None = None,
    maxMileage: int | None = None,
    minRange: int | None = None,
    minBattery: int | None = None,
    verified: bool | None = None,
    sort: str | None = None,
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    per_page: Annotated[int, Query(ge=1, le=48)] = 24,
) -> dict[str, Any]:
    return await service.list(
        {
            "q": q,
            "make": make,
            "model": model,
            "body": body,
            "condition": condition,
            "location": location,
            "mode": mode,
            "minPrice": minPrice,
            "maxPrice": maxPrice,
            "minYear": minYear,
            "maxYear": maxYear,
            "maxMileage": maxMileage,
            "minRange": minRange,
            "minBattery": minBattery,
            "verified": verified,
            "sort": sort,
            "page": page,
            "per_page": per_page,
        }
    )


@router.get("/facets")
async def facets(service: VehicleServiceDep) -> dict[str, Any]:
    return await service.facets()


@router.get("/sitemap")
async def sitemap(
    service: VehicleServiceDep, page: Annotated[int, Query(ge=1)] = 1
) -> dict[str, Any]:
    return await service.sitemap(page=page)


class CompareRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=4)


@router.post("/compare")
async def compare(body: CompareRequest, service: VehicleServiceDep) -> list[dict[str, Any]]:
    return await service.compare(body.ids)


# Declared after /facets, /sitemap and /compare so those literal paths are not
# swallowed by the slug parameter.
@router.get("/by-slug/{slug}")
async def get_by_slug(slug: str, service: VehicleServiceDep) -> dict[str, Any]:
    return await service.get_by_slug(slug)


@router.get("/{vehicle_id}/similar")
async def similar(
    vehicle_id: str, service: VehicleServiceDep, limit: Annotated[int, Query(ge=1, le=12)] = 6
) -> list[dict[str, Any]]:
    return await service.similar(vehicle_id, limit=limit)
