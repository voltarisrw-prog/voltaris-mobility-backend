"""Payment state machine and stored shape."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


#: PAID is terminal except for a refund. A second `payment.succeeded` webhook can
#: never move PAID -> PAID, which is what makes double-crediting impossible even if
#: the duplicate-event index were somehow bypassed.
ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset(
        {
            PaymentStatus.PROCESSING,
            PaymentStatus.PAID,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.PROCESSING: frozenset(
        {PaymentStatus.PAID, PaymentStatus.FAILED, PaymentStatus.CANCELLED}
    ),
    PaymentStatus.PAID: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.FAILED: frozenset({PaymentStatus.PROCESSING}),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class PaymentDocument(BaseModel):
    id: str = Field(alias="_id")
    order_id: str
    customer_id: str
    provider: str
    provider_transaction_id: str | None = None
    amount: int
    currency: str
    status: PaymentStatus = PaymentStatus.PENDING
    idempotency_key: str | None = None
    failure_code: str | None = None
    version: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"populate_by_name": True}


class PaymentEventDocument(BaseModel):
    """Immutable record of every webhook received, valid or not.

    Kept even for rejected events: a burst of signature failures is the signal that
    someone is probing the endpoint, and it is invisible if only successes are stored.
    """

    id: str = Field(alias="_id")
    provider: str
    provider_event_id: str
    event_type: str
    payment_id: str | None = None
    order_id: str | None = None
    signature_valid: bool
    processed: bool = False
    outcome: str | None = None
    raw_payload: dict = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"populate_by_name": True}
