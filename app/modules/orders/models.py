"""Order state machine and stored shape."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class OrderKind(StrEnum):
    PURCHASE = "purchase"
    RENTAL = "rental"
    RESERVATION = "reservation"


#: The only transitions that exist. Anything not listed is rejected, including
#: same-state writes, so a duplicate confirmation cannot re-run side effects.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED}),
    OrderStatus.PAYMENT_PENDING: frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID: frozenset({OrderStatus.PROCESSING, OrderStatus.REFUNDED}),
    OrderStatus.PROCESSING: frozenset({OrderStatus.COMPLETED, OrderStatus.REFUNDED}),
    OrderStatus.COMPLETED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REFUNDED: frozenset(),
}

#: Statuses in which an order still holds its vehicle. Mirrors the partial unique
#: index in indexes.py — the two must stay in step, which the tests assert.
ACTIVE_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.PAYMENT_PENDING,
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
)


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class OrderLine(BaseModel):
    label: str
    amount: int


class OrderDocument(BaseModel):
    id: str = Field(alias="_id")
    reference: str
    customer_id: str
    vehicle_id: str
    vehicle_slug: str
    vehicle_title: str
    kind: OrderKind = OrderKind.PURCHASE

    lines: list[OrderLine] = Field(default_factory=list)
    #: Authoritative, computed server-side from the vehicle. Never client-supplied.
    total: int
    currency: str

    status: OrderStatus = OrderStatus.PENDING
    payment_id: str | None = None
    version: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"populate_by_name": True}
