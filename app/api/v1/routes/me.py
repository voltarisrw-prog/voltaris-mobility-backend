"""The account area. Every route is scoped to the authenticated principal."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, ProfileServiceDep

router = APIRouter(prefix="/me", tags=["account"])


class ProfileUpdate(BaseModel):
    """No `email`, `roles`, or `email_verified`. Changing an email needs
    verification, and the other two are not the account holder's to set."""

    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    preferred_language: str | None = Field(default=None, pattern="^(en|fr|rw)$")
    marketing_opt_in: bool | None = None


@router.get("")
async def get_profile(principal: CurrentUser, service: ProfileServiceDep) -> dict[str, Any]:
    return await service.get(principal.user_id)


@router.patch("")
async def update_profile(
    body: ProfileUpdate, principal: CurrentUser, service: ProfileServiceDep
) -> dict[str, Any]:
    return await service.update(principal.user_id, body.model_dump(exclude_none=True))


@router.get("/saved-vehicles")
async def saved_vehicles(principal: CurrentUser, service: ProfileServiceDep) -> dict[str, Any]:
    return await service.saved_vehicles(principal.user_id)


@router.put("/saved-vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def save_vehicle(vehicle_id: str, principal: CurrentUser, service: ProfileServiceDep) -> None:
    # PUT, not POST: saving an already-saved vehicle is a no-op, not a duplicate.
    await service.save_vehicle(principal.user_id, vehicle_id)


@router.delete("/saved-vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_vehicle(
    vehicle_id: str, principal: CurrentUser, service: ProfileServiceDep
) -> None:
    await service.unsave_vehicle(principal.user_id, vehicle_id)


@router.get("/saved-searches")
async def saved_searches(
    principal: CurrentUser, service: ProfileServiceDep
) -> list[dict[str, Any]]:
    return await service.saved_searches(principal.user_id)


class SavedSearchRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    query: str = Field(max_length=500)


@router.post("/saved-searches", status_code=status.HTTP_201_CREATED)
async def create_saved_search(
    body: SavedSearchRequest, principal: CurrentUser, service: ProfileServiceDep
) -> dict[str, Any]:
    return await service.create_saved_search(principal.user_id, body.label, body.query)


@router.delete("/saved-searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_search(
    search_id: str, principal: CurrentUser, service: ProfileServiceDep
) -> None:
    await service.delete_saved_search(principal.user_id, search_id)


@router.get("/inquiries")
async def my_inquiries(principal: CurrentUser, service: ProfileServiceDep) -> dict[str, Any]:
    return await service.inquiries(principal.user_id)


@router.get("/test-drives")
async def my_test_drives(principal: CurrentUser, service: ProfileServiceDep) -> dict[str, Any]:
    return await service.test_drives(principal.user_id)


@router.get("/notifications")
async def my_notifications(principal: CurrentUser, service: ProfileServiceDep) -> dict[str, Any]:
    return await service.notifications(principal.user_id)
