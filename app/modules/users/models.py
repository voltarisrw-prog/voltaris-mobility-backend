"""User domain types and the role model."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, EmailStr, Field


class Role(StrEnum):
    BUYER = "BUYER"
    SELLER = "SELLER"
    DEALER = "DEALER"
    SALES_AGENT = "SALES_AGENT"
    FINANCE = "FINANCE"
    CONTENT_MANAGER = "CONTENT_MANAGER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class Permission(StrEnum):
    VEHICLE_READ_INTERNAL = "vehicle:read_internal"
    VEHICLE_WRITE = "vehicle:write"
    VEHICLE_APPROVE = "vehicle:approve"
    ORDER_READ_ANY = "order:read_any"
    PAYMENT_READ_ANY = "payment:read_any"
    PAYMENT_REFUND = "payment:refund"
    COMMISSION_READ = "commission:read"
    COMMISSION_WRITE = "commission:write"
    SETTLEMENT_WRITE = "settlement:write"
    USER_MANAGE = "user:manage"
    ROLE_ASSIGN = "role:assign"
    AUDIT_READ = "audit:read"
    SYSTEM_INSPECT = "system:inspect"
    SYSTEM_MONITOR = "system:monitor"
    CONFIG_WRITE = "config:write"


#: The authorization matrix. Deliberately explicit rather than hierarchical —
#: "FINANCE can refund but cannot change configuration" is a business rule, and a
#: role hierarchy would quietly grant it the moment someone reordered the levels.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.BUYER: frozenset(),
    Role.SELLER: frozenset(),
    Role.DEALER: frozenset({Permission.VEHICLE_WRITE}),
    Role.SALES_AGENT: frozenset(
        {
            Permission.VEHICLE_READ_INTERNAL,
            Permission.VEHICLE_WRITE,
            Permission.ORDER_READ_ANY,
        }
    ),
    Role.FINANCE: frozenset(
        {
            Permission.ORDER_READ_ANY,
            Permission.PAYMENT_READ_ANY,
            Permission.PAYMENT_REFUND,
            Permission.COMMISSION_READ,
            Permission.COMMISSION_WRITE,
            Permission.SETTLEMENT_WRITE,
            Permission.VEHICLE_READ_INTERNAL,
            Permission.AUDIT_READ,
        }
    ),
    Role.CONTENT_MANAGER: frozenset(),
    Role.ADMIN: frozenset(
        {
            Permission.VEHICLE_READ_INTERNAL,
            Permission.VEHICLE_WRITE,
            Permission.VEHICLE_APPROVE,
            Permission.ORDER_READ_ANY,
            Permission.PAYMENT_READ_ANY,
            Permission.COMMISSION_READ,
            Permission.USER_MANAGE,
            Permission.AUDIT_READ,
            Permission.SYSTEM_MONITOR,
        }
    ),
    # Note what ADMIN does NOT have: PAYMENT_REFUND, COMMISSION_WRITE,
    # SETTLEMENT_WRITE, ROLE_ASSIGN, CONFIG_WRITE, SYSTEM_INSPECT. Moving money,
    # granting privilege, and reading raw collections are all separated from general
    # administration on purpose.
    Role.SUPER_ADMIN: frozenset(Permission),
}


def permissions_for(roles: list[str]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for raw in roles:
        try:
            granted |= ROLE_PERMISSIONS[Role(raw)]
        except ValueError:
            # An unknown role grants nothing. Fail closed.
            continue
    return frozenset(granted)


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class UserDocument(BaseModel):
    """The stored shape. Never serialised to a client — see schemas.py."""

    id: str = Field(alias="_id")
    name: str
    email: EmailStr
    phone: str | None = None
    password_hash: str
    roles: list[Role] = Field(default_factory=lambda: [Role.BUYER])
    status: UserStatus = UserStatus.PENDING_VERIFICATION
    email_verified: bool = False
    phone_verified: bool = False
    mfa_enabled: bool = False
    mfa_secret: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    model_config = {"populate_by_name": True}
