"""Super-admin console routes. Every one requires SYSTEM_INSPECT or SYSTEM_MONITOR,
which only SUPER_ADMIN holds — see the permission matrix in modules/users/models.py.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import AdminConsoleDep, CurrentUser, Principal, require_permission
from app.modules.users.models import Permission, Role, UserStatus

router = APIRouter(prefix="/console", tags=["super-admin"])

Monitor = Annotated[Principal, Depends(require_permission(Permission.SYSTEM_MONITOR))]
Inspector = Annotated[Principal, Depends(require_permission(Permission.SYSTEM_INSPECT))]


@router.get("/overview")
async def overview(principal: Monitor, console: AdminConsoleDep) -> dict[str, Any]:
    _ = principal
    return await console.overview()


@router.get("/collections")
async def collections(principal: Inspector, console: AdminConsoleDep) -> list[dict[str, Any]]:
    _ = principal
    return await console.collection_stats()


class InspectRequest(BaseModel):
    """A read-only query.

    There is no update, delete, aggregate, or command equivalent, and the filter is
    validated against an operator allow-list — `$where`, `$expr`, and `$function` are
    rejected, because each turns a read endpoint into arbitrary execution.
    """

    collection: str = Field(min_length=1, max_length=64)
    query: dict[str, Any] = Field(default_factory=dict)
    sort_field: str = Field(default="_id", max_length=64)
    descending: bool = True
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/inspect")
async def inspect(
    body: InspectRequest, principal: Inspector, console: AdminConsoleDep
) -> dict[str, Any]:
    return await console.inspect(
        collection=body.collection,
        query=body.query,
        sort_field=body.sort_field,
        descending=body.descending,
        limit=body.limit,
        actor_id=principal.user_id,
    )


@router.get("/history/{entity_type}/{entity_id}")
async def entity_history(
    entity_type: str,
    entity_id: str,
    principal: Inspector,
    console: AdminConsoleDep,
) -> list[dict[str, Any]]:
    _ = principal
    return await console.entity_history(entity_type=entity_type, entity_id=entity_id)


class StatusChange(BaseModel):
    status: UserStatus
    reason: str = Field(min_length=3, max_length=500)


@router.post("/users/{user_id}/status")
async def set_status(
    user_id: str, body: StatusChange, principal: Inspector, console: AdminConsoleDep
) -> dict[str, Any]:
    return await console.set_user_status(
        user_id=user_id, status=body.status, actor_id=principal.user_id, reason=body.reason
    )


class RoleChange(BaseModel):
    roles: list[Role] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)


@router.post("/users/{user_id}/roles")
async def set_roles(
    user_id: str, body: RoleChange, principal: Inspector, console: AdminConsoleDep
) -> dict[str, Any]:
    return await console.set_user_roles(
        user_id=user_id, roles=body.roles, actor_id=principal.user_id, reason=body.reason
    )


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: str, principal: Inspector, console: AdminConsoleDep
) -> dict[str, Any]:
    return await console.revoke_all_sessions(user_id=user_id, actor_id=principal.user_id)


_ = CurrentUser, Query
