"""Vehicle domain types.

The financial fields live in a nested `internal` document rather than at the top
level. That is a deliberate structural choice: the public projection is
`{"internal": 0}`, one line that cannot be partially forgotten, instead of a
denylist of individual field names that grows every time someone adds a column.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class VehicleStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    REJECTED = "REJECTED"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    SOLD = "SOLD"
    UNPUBLISHED = "UNPUBLISHED"


#: Statuses a listing must be in to be visible on the public marketplace.
PUBLIC_STATUSES = (VehicleStatus.AVAILABLE, VehicleStatus.RESERVED, VehicleStatus.SOLD)


class BodyType(StrEnum):
    SUV = "suv"
    SEDAN = "sedan"
    HATCHBACK = "hatchback"
    PICKUP = "pickup"
    VAN = "van"
    MOTORCYCLE = "motorcycle"
    BUS = "bus"


class Condition(StrEnum):
    NEW = "new"
    USED = "used"
    CERTIFIED = "certified"


class InternalFinancials(BaseModel):
    """Never leaves the server except through an explicitly authorised schema."""

    #: What the seller wants, in minor units.
    seller_expected_price: int
    #: Basis points Voltaris takes. Overrides the platform default for this listing.
    commission_bps: int | None = None
    internal_notes: str | None = None
    acquisition_cost: int | None = None


class VehicleLocation(BaseModel):
    city: str
    district: str | None = None
    slug: str


class VehicleImage(BaseModel):
    thumb: str
    card: str
    detail: str
    gallery: str
    width: int
    height: int
    alt: str
    blur_data_url: str | None = None


class ChargingSpec(BaseModel):
    ac_kw: float
    dc_kw: float | None = None
    port_type: str
    dc_10_80_minutes: int | None = None


class VehicleDocument(BaseModel):
    id: str = Field(alias="_id")
    slug: str
    dealer_id: str | None = None
    owner_id: str | None = None

    make: str
    make_slug: str
    model: str
    variant: str | None = None
    year: int
    condition: Condition
    body_type: BodyType
    mileage_km: int

    #: The customer-facing price, in minor units. The only price a public schema
    #: may expose, and the only one an order may be priced from.
    agency_price: int | None = None
    currency: str = "RWF"
    rental_price_per_day: int | None = None

    battery_kwh: float
    range_km: int
    power_kw: int
    charging: ChargingSpec
    seats: int = 5
    doors: int = 5
    drivetrain: str = "fwd"

    location: VehicleLocation
    description: str = ""
    features: list[str] = Field(default_factory=list)
    images: list[VehicleImage] = Field(default_factory=list)

    status: VehicleStatus = VehicleStatus.DRAFT
    verified: bool = False
    purchase_enabled: bool = False
    rental_enabled: bool = False
    test_drive_available: bool = True

    internal: InternalFinancials

    #: Optimistic concurrency. Every write asserts the version it read.
    version: int = 0
    published_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    model_config = {"populate_by_name": True}


#: Projection applied to every public read. One rule, applied centrally.
PUBLIC_PROJECTION = {"internal": 0}
