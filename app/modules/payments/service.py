"""Payments. The provider is authoritative; the client is never consulted.

The only way a payment becomes PAID is a signed webhook from the provider that
passes, in order: signature verification, replay check, order match, amount match,
currency match, and a legal state transition. Every one of those is a separate gate
and every failure is recorded.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import verify_webhook_signature
from app.infrastructure.database.client import Collections
from app.modules.audit.service import AuditService
from app.modules.commissions.service import CommissionStatus
from app.modules.commissions.service import calculate as calculate_commission
from app.modules.orders.models import OrderStatus
from app.modules.orders.service import OrderService
from app.modules.payments.models import PaymentStatus, can_transition

logger = logging.getLogger("voltaris.payments")


class PaymentProvider(Protocol):
    """The integration boundary.

    No real provider is wired up. This interface is what a provider adapter must
    satisfy; `StubProvider` below implements it for development and tests and is
    refused in production by `get_provider`.
    """

    name: str

    async def create_checkout_session(
        self, *, order_id: str, amount: int, currency: str, customer_email: str
    ) -> dict[str, Any]: ...

    async def fetch_transaction(self, provider_transaction_id: str) -> dict[str, Any]: ...


class StubProvider:
    """Development stand-in. Creates a session reference and nothing else.

    It cannot mark anything paid. Advancing a payment still requires a correctly
    signed webhook, so the development path exercises the same gates as production.
    """

    name = "stub"

    async def create_checkout_session(
        self, *, order_id: str, amount: int, currency: str, customer_email: str
    ) -> dict[str, Any]:
        _ = customer_email
        reference = f"stub_txn_{uuid.uuid4().hex[:16]}"
        return {
            "provider_transaction_id": reference,
            "redirect_url": f"https://payments.invalid/checkout/{reference}",
            "expires_at": datetime.now(UTC).isoformat(),
            "amount": amount,
            "currency": currency,
            "order_id": order_id,
        }

    async def fetch_transaction(self, provider_transaction_id: str) -> dict[str, Any]:
        return {"id": provider_transaction_id, "status": "unknown"}


def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.payment_provider == "stub":
        if settings.is_production:
            # A stub provider in production would accept orders that can never be paid.
            raise AppError(
                ErrorCode.NOT_CONFIGURED,
                detail="PAYMENT_PROVIDER is still 'stub' in production",
            )
        return StubProvider()
    raise AppError(
        ErrorCode.NOT_CONFIGURED,
        detail=f"no adapter implemented for provider '{settings.payment_provider}'",
    )


class PaymentService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        audit: AuditService,
        orders: OrderService,
        provider: PaymentProvider,
    ) -> None:
        self._db = db
        self._audit = audit
        self._orders = orders
        self._provider = provider
        self._settings = get_settings()

    # -- initiation --------------------------------------------------------

    async def create_checkout_session(
        self, *, order_id: str, customer_id: str, customer_email: str
    ) -> dict[str, Any]:
        order = await self._db[Collections.ORDERS].find_one(
            {"_id": order_id, "customer_id": customer_id}
        )
        if order is None:
            raise AppError(ErrorCode.ORDER_NOT_FOUND, detail=order_id)

        status = OrderStatus(order["status"])
        if status is OrderStatus.PAID:
            raise AppError(ErrorCode.PAYMENT_ALREADY_PROCESSED, detail=f"order {order_id} is PAID")
        if status not in (OrderStatus.PENDING, OrderStatus.PAYMENT_PENDING):
            raise AppError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"cannot pay an order in {status}",
            )

        # Reuse an in-flight payment rather than opening a second one. Two open
        # payments against one order is how double charges happen.
        existing = await self._db[Collections.PAYMENTS].find_one(
            {
                "order_id": order_id,
                "status": {"$in": [PaymentStatus.PENDING.value, PaymentStatus.PROCESSING.value]},
            }
        )
        if existing is not None:
            session = await self._provider.create_checkout_session(
                order_id=order_id,
                amount=existing["amount"],
                currency=existing["currency"],
                customer_email=customer_email,
            )
            return {"payment_id": existing["_id"], **session}

        payment_id = uuid.uuid4().hex
        # Amount comes from the order, which came from the vehicle. At no point does
        # a client-supplied number enter this path.
        session = await self._provider.create_checkout_session(
            order_id=order_id,
            amount=order["total"],
            currency=order["currency"],
            customer_email=customer_email,
        )

        now = datetime.now(UTC)
        await self._db[Collections.PAYMENTS].insert_one(
            {
                "_id": payment_id,
                "order_id": order_id,
                "customer_id": customer_id,
                "provider": self._provider.name,
                "provider_transaction_id": session["provider_transaction_id"],
                "amount": order["total"],
                "currency": order["currency"],
                "status": PaymentStatus.PENDING.value,
                "version": 0,
                "created_at": now,
                "updated_at": now,
            }
        )

        if OrderStatus(order["status"]) is OrderStatus.PENDING:
            await self._orders.transition(
                order_id=order_id, target=OrderStatus.PAYMENT_PENDING, actor_id=customer_id
            )

        await self._db[Collections.ORDERS].update_one(
            {"_id": order_id}, {"$set": {"payment_id": payment_id}}
        )
        await self._audit.record(
            action="payment.session_created",
            entity_type="payment",
            entity_id=payment_id,
            actor_id=customer_id,
            after={"order_id": order_id, "amount": order["total"], "status": "PENDING"},
        )
        return {"payment_id": payment_id, **session}

    # -- webhook -----------------------------------------------------------

    async def handle_webhook(
        self, *, raw_body: bytes, signature_header: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Process one provider event. Every gate is checked before any state moves."""
        provider_event_id = str(payload.get("id") or "")
        event_type = str(payload.get("type") or "")

        # GATE 1 — signature. Recorded before raising, so probing is visible.
        try:
            verify_webhook_signature(
                payload=raw_body,
                signature_header=signature_header,
                secret=self._settings.payment_webhook_secret,
                tolerance_seconds=self._settings.payment_webhook_tolerance_seconds,
            )
        except AppError:
            await self._record_event(
                provider_event_id=provider_event_id or f"unsigned_{uuid.uuid4().hex}",
                event_type=event_type,
                signature_valid=False,
                outcome="rejected: signature",
                payload=payload,
            )
            raise

        if not provider_event_id:
            raise AppError(ErrorCode.INVALID_REQUEST, detail="event has no id")

        # GATE 2 — replay, in two layers.
        #
        # The cheap read catches the ordinary case: a provider redelivering an event
        # it already sent, seconds or hours later. The unique index catches the case
        # the read cannot — two deliveries racing each other, where both reads miss
        # before either write lands. Neither layer alone is sufficient: the read has
        # a window, and relying only on the index means a routine retry costs a
        # failed write and a log line.
        already = await self._db[Collections.PAYMENT_EVENTS].find_one(
            {"provider": self._provider.name, "provider_event_id": provider_event_id}
        )
        if already is not None:
            # 200, so the provider stops retrying. From its side this succeeded.
            return {"status": "duplicate", "event_id": provider_event_id}

        try:
            event_row_id = await self._record_event(
                provider_event_id=provider_event_id,
                event_type=event_type,
                signature_valid=True,
                outcome=None,
                payload=payload,
            )
        except DuplicateKeyError:
            # Lost a race with a concurrent delivery of the same event.
            return {"status": "duplicate", "event_id": provider_event_id}

        data = payload.get("data") or {}
        payment_id = str(data.get("payment_id") or "")
        payment = await self._db[Collections.PAYMENTS].find_one({"_id": payment_id})
        if payment is None:
            await self._finish_event(event_row_id, "rejected: unknown payment")
            raise AppError(ErrorCode.PAYMENT_NOT_FOUND, detail=payment_id)

        # GATE 3/4 — amount and currency must match what we recorded. A provider
        # reporting a different figure means tampering or a provider-side bug; either
        # way it must not be recorded as settling this order.
        if event_type == "payment.succeeded":
            reported_amount = int(data.get("amount", -1))
            if reported_amount != payment["amount"]:
                await self._finish_event(event_row_id, "rejected: amount mismatch")
                await self._audit.record(
                    action="payment.amount_mismatch",
                    entity_type="payment",
                    entity_id=payment_id,
                    before={"expected": payment["amount"]},
                    after={"reported": reported_amount},
                )
                raise AppError(
                    ErrorCode.PAYMENT_AMOUNT_MISMATCH,
                    detail=f"expected {payment['amount']}, provider reported {reported_amount}",
                )
            if str(data.get("currency", "")) != payment["currency"]:
                await self._finish_event(event_row_id, "rejected: currency mismatch")
                raise AppError(
                    ErrorCode.PAYMENT_CURRENCY_MISMATCH,
                    detail=f"expected {payment['currency']}",
                )

        target = {
            "payment.succeeded": PaymentStatus.PAID,
            "payment.failed": PaymentStatus.FAILED,
            "payment.cancelled": PaymentStatus.CANCELLED,
            "payment.refunded": PaymentStatus.REFUNDED,
            "payment.processing": PaymentStatus.PROCESSING,
        }.get(event_type)
        if target is None:
            await self._finish_event(event_row_id, "ignored: unhandled type")
            return {"status": "ignored", "event_id": provider_event_id}

        # GATE 5 — the state machine.
        current = PaymentStatus(payment["status"])
        if not can_transition(current, target):
            await self._finish_event(event_row_id, f"rejected: {current} -> {target}")
            raise AppError(
                ErrorCode.INVALID_STATE_TRANSITION,
                detail=f"payment {payment_id}: {current} -> {target}",
            )

        updated = await self._db[Collections.PAYMENTS].find_one_and_update(
            {"_id": payment_id, "status": current.value, "version": payment["version"]},
            {
                "$set": {
                    "status": target.value,
                    "updated_at": datetime.now(UTC),
                    "provider_transaction_id": data.get(
                        "provider_transaction_id", payment.get("provider_transaction_id")
                    ),
                },
                "$inc": {"version": 1},
            },
            return_document=True,
        )
        if updated is None:
            await self._finish_event(event_row_id, "rejected: concurrent modification")
            raise AppError(ErrorCode.CONCURRENT_MODIFICATION, detail=payment_id)

        await self._audit.record(
            action="payment.status_changed",
            entity_type="payment",
            entity_id=payment_id,
            before={"status": current.value},
            after={"status": target.value, "event_id": provider_event_id},
        )

        if target is PaymentStatus.PAID:
            await self._settle(payment)

        await self._finish_event(event_row_id, f"applied: {target.value}")
        return {"status": "applied", "payment_status": target.value, "event_id": provider_event_id}

    async def _settle(self, payment: dict[str, Any]) -> None:
        """Advance the order and write the commission ledger entry."""
        order_id = payment["order_id"]
        await self._orders.transition(
            order_id=order_id, target=OrderStatus.PAID, actor_id=None, reason="payment settled"
        )

        order = await self._db[Collections.ORDERS].find_one({"_id": order_id})
        if order is None:
            return
        vehicle = await self._db[Collections.VEHICLES].find_one({"_id": order["vehicle_id"]})
        if vehicle is None:
            return

        internal = vehicle.get("internal") or {}
        try:
            breakdown = calculate_commission(
                gross_sale=order["total"],
                seller_expected_price=int(internal.get("seller_expected_price", 0)),
                currency=order["currency"],
                commission_bps=int(
                    internal.get("commission_bps") or self._settings.default_commission_bps
                ),
                payment_fee_bps=self._settings.payment_fee_bps,
                payment_fee_fixed_minor=self._settings.payment_fee_fixed_minor,
            )
            document = breakdown.as_document(order_id)
        except ValueError as exc:
            # The customer has already paid and the provider considers this settled.
            # Refusing here would 500 the webhook, trigger endless redelivery, and
            # leave a paid order with no financial record at all. Instead the sale is
            # booked as unallocated for finance to resolve by hand, and alerted on.
            logger.error(
                "commission could not be computed for a settled payment",
                extra={
                    "order_id": order_id,
                    "vehicle_id": order["vehicle_id"],
                    "gross_sale": order["total"],
                    "reason": str(exc),
                },
            )
            document = {
                "order_id": order_id,
                "gross_sale": order["total"],
                "owner_settlement": None,
                "agency_commission": None,
                "payment_fees": None,
                "net_revenue": None,
                "currency": order["currency"],
                "commission_bps": None,
                "status": CommissionStatus.NEEDS_REVIEW.value,
                "review_reason": str(exc),
                "created_at": datetime.now(UTC),
            }
            await self._audit.record(
                action="commission.needs_review",
                entity_type="commission",
                entity_id=order_id,
                after={"gross_sale": order["total"], "reason": str(exc)},
            )

        try:
            document["_id"] = uuid.uuid4().hex
            await self._db[Collections.COMMISSIONS].insert_one(document)
        except DuplicateKeyError:
            # Unique on order_id: a redelivered event can never book revenue twice.
            return

        if document["status"] == CommissionStatus.NEEDS_REVIEW.value:
            return

        await self._audit.record(
            action="commission.recorded",
            entity_type="commission",
            entity_id=order_id,
            after={
                "gross_sale": breakdown.gross_sale,
                "owner_settlement": breakdown.owner_settlement,
                "agency_commission": breakdown.agency_commission,
                "net_revenue": breakdown.net_revenue,
            },
        )

    # -- event log ---------------------------------------------------------

    async def _record_event(
        self,
        *,
        provider_event_id: str,
        event_type: str,
        signature_valid: bool,
        outcome: str | None,
        payload: dict[str, Any],
    ) -> str:
        row_id = uuid.uuid4().hex
        await self._db[Collections.PAYMENT_EVENTS].insert_one(
            {
                "_id": row_id,
                "provider": self._provider.name,
                "provider_event_id": provider_event_id,
                "event_type": event_type,
                "signature_valid": signature_valid,
                "processed": outcome is not None,
                "outcome": outcome,
                "raw_payload": payload,
                "received_at": datetime.now(UTC),
            }
        )
        return row_id

    async def _finish_event(self, row_id: str, outcome: str) -> None:
        await self._db[Collections.PAYMENT_EVENTS].update_one(
            {"_id": row_id}, {"$set": {"processed": True, "outcome": outcome}}
        )

    # -- reads -------------------------------------------------------------

    async def get_state_for_customer(self, *, order_id: str, customer_id: str) -> dict[str, Any]:
        payment = await self._db[Collections.PAYMENTS].find_one(
            {"order_id": order_id, "customer_id": customer_id},
            sort=[("created_at", -1)],
        )
        if payment is None:
            raise AppError(ErrorCode.PAYMENT_NOT_FOUND, detail=f"order {order_id}")
        return {
            "order_id": order_id,
            "state": payment["status"],
            "amount": payment["amount"],
            "currency": payment["currency"],
            "provider_reference": payment.get("provider_transaction_id"),
            "updated_at": payment["updated_at"].isoformat(),
        }
