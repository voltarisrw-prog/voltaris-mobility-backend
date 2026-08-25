"""Request-scoped dependencies: identity, authorization, and service wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.errors import AppError, ErrorCode
from app.core.logging import user_id_var
from app.core.security import decode_token
from app.core.sessions import enforce_csrf, extract_token
from app.infrastructure.database.client import Collections, get_db
from app.infrastructure.storage.r2 import get_storage
from app.modules.admin.console import AdminConsoleService
from app.modules.audit.service import AuditService
from app.modules.auth.service import AuthService
from app.modules.charging.service import ChargingService
from app.modules.content.service import ContentService
from app.modules.dealers.service import DealerService
from app.modules.leads.service import LeadService
from app.modules.media.service import MediaService
from app.modules.orders.service import OrderService
from app.modules.payments.service import PaymentService, get_provider
from app.modules.users.models import Permission, Role, UserStatus, permissions_for
from app.modules.users.profile_service import ProfileService
from app.modules.vehicles.service import VehicleService


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: tuple[str, ...]
    session_id: str
    permissions: frozenset[Permission]
    email: str
    full_name: str
    email_verified: bool
    mfa_enabled: bool

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AppError(
                ErrorCode.FORBIDDEN,
                detail=f"user {self.user_id} lacks {permission}",
                context={"required": permission.value},
            )


def db_dep() -> AsyncIOMotorDatabase:
    return get_db()


DbDep = Annotated[AsyncIOMotorDatabase, Depends(db_dep)]


def audit_dep(db: DbDep) -> AuditService:
    return AuditService(db)


AuditDep = Annotated[AuditService, Depends(audit_dep)]


def auth_service_dep(db: DbDep, audit: AuditDep) -> AuthService:
    return AuthService(db, audit)


def order_service_dep(db: DbDep, audit: AuditDep) -> OrderService:
    return OrderService(db, audit)


def payment_service_dep(db: DbDep, audit: AuditDep) -> PaymentService:
    return PaymentService(db, audit, OrderService(db, audit), get_provider())


def admin_console_dep(db: DbDep, audit: AuditDep) -> AdminConsoleService:
    return AdminConsoleService(db, audit)


def vehicle_service_dep(db: DbDep) -> VehicleService:
    return VehicleService(db)


def dealer_service_dep(db: DbDep) -> DealerService:
    return DealerService(db)


def content_service_dep(db: DbDep) -> ContentService:
    return ContentService(db)


def charging_service_dep(db: DbDep) -> ChargingService:
    return ChargingService(db)


def lead_service_dep(db: DbDep, audit: AuditDep) -> LeadService:
    return LeadService(db, audit)


def media_service_dep(db: DbDep, audit: AuditDep) -> MediaService:
    return MediaService(db, get_storage(), audit)


def profile_service_dep(db: DbDep) -> ProfileService:
    return ProfileService(db)


VehicleServiceDep = Annotated[VehicleService, Depends(vehicle_service_dep)]
DealerServiceDep = Annotated[DealerService, Depends(dealer_service_dep)]
ContentServiceDep = Annotated[ContentService, Depends(content_service_dep)]
ChargingServiceDep = Annotated[ChargingService, Depends(charging_service_dep)]
LeadServiceDep = Annotated[LeadService, Depends(lead_service_dep)]
ProfileServiceDep = Annotated[ProfileService, Depends(profile_service_dep)]
MediaServiceDep = Annotated[MediaService, Depends(media_service_dep)]

AuthServiceDep = Annotated[AuthService, Depends(auth_service_dep)]
AdminConsoleDep = Annotated[AdminConsoleService, Depends(admin_console_dep)]
OrderServiceDep = Annotated[OrderService, Depends(order_service_dep)]
PaymentServiceDep = Annotated[PaymentService, Depends(payment_service_dep)]


async def current_principal(
    request: Request,
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the caller.

    Three checks, all server-side: the token verifies, the session has not been
    revoked, and the user is still active. Roles are re-read from the database on
    every request rather than trusted from the token, so revoking a role takes effect
    immediately instead of at the next token expiry.
    """
    token, from_cookie = extract_token(request, authorization)
    if from_cookie:
        # Cookies ride along automatically, so a cookie-authenticated mutation needs
        # the double-submit token. Bearer requests do not.
        enforce_csrf(request)

    claims = decode_token(token, expected_type="access")

    session = await db[Collections.SESSIONS].find_one({"session_id": claims.session_id})
    if session is None or session.get("revoked_at") is not None:
        raise AppError(ErrorCode.TOKEN_REVOKED, detail="session revoked")

    user = await db[Collections.USERS].find_one({"_id": claims.subject})
    if user is None or user.get("deleted_at") is not None:
        raise AppError(ErrorCode.TOKEN_INVALID, detail="user gone")
    if user["status"] == UserStatus.SUSPENDED.value:
        raise AppError(ErrorCode.FORBIDDEN, detail="account suspended")

    roles = [str(role) for role in user["roles"]]
    user_id_var.set(user["_id"])
    return Principal(
        user_id=user["_id"],
        roles=tuple(roles),
        session_id=claims.session_id,
        permissions=permissions_for(roles),
        email=user["email"],
        full_name=user.get("name") or user.get("full_name") or "",
        email_verified=bool(user.get("email_verified")),
        mfa_enabled=bool(user.get("mfa_enabled")),
    )


CurrentUser = Annotated[Principal, Depends(current_principal)]


def require_permission(permission: Permission):
    """Route guard factory. Authorization is asserted here, never in the frontend."""

    async def guard(principal: CurrentUser) -> Principal:
        principal.require(permission)
        return principal

    return guard


def require_role(*roles: Role):
    async def guard(principal: CurrentUser) -> Principal:
        if not set(principal.roles) & {role.value for role in roles}:
            raise AppError(
                ErrorCode.FORBIDDEN,
                detail=f"user {principal.user_id} has {principal.roles}, needs one of {roles}",
            )
        return principal

    return guard


async def optional_principal(
    request: Request,
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Resolve the caller if there is one, otherwise None.

    Enquiries and test drives are open to anonymous visitors — requiring an
    account before someone can ask a question loses exactly the lead worth
    having. But when a session *is* present the record should be attached to it,
    so the person sees it later under "my enquiries". A failed or absent token
    is not an error here; it just means anonymous.
    """
    try:
        return await current_principal(request, db, authorization)
    except AppError:
        return None


OptionalUser = Annotated[Principal | None, Depends(optional_principal)]
