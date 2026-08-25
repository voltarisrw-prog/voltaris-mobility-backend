from __future__ import annotations

from app.infrastructure.database.client import Collections
from app.infrastructure.database.indexes import INDEXES
from app.modules.orders.models import ACTIVE_ORDER_STATUSES, OrderStatus
from app.modules.orders.models import can_transition as order_can
from app.modules.payments.models import PaymentStatus
from app.modules.payments.models import can_transition as payment_can


def test_terminal_order_states_are_terminal():
    for terminal in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        for target in OrderStatus:
            assert not order_can(terminal, target)


def test_order_cannot_skip_payment():
    assert not order_can(OrderStatus.PENDING, OrderStatus.PAID)
    assert not order_can(OrderStatus.PENDING, OrderStatus.COMPLETED)


def test_no_state_transitions_to_itself():
    # Self-transitions would let a redelivered event re-run side effects.
    for status in OrderStatus:
        assert not order_can(status, status)
    for status in PaymentStatus:
        assert not payment_can(status, status)


def test_paid_payment_can_only_be_refunded():
    for target in PaymentStatus:
        expected = target is PaymentStatus.REFUNDED
        assert payment_can(PaymentStatus.PAID, target) is expected


def test_refunded_is_absorbing():
    for target in PaymentStatus:
        assert not payment_can(PaymentStatus.REFUNDED, target)


def test_active_order_statuses_match_the_partial_index():
    """The concurrency guard is split across two files; they must agree.

    If someone adds a status to ACTIVE_ORDER_STATUSES without updating the partial
    filter, the unique index silently stops covering it and double-ordering becomes
    possible. This test is the tripwire.
    """
    index = next(
        model
        for model in INDEXES[Collections.ORDERS]
        if model.document["name"] == "uniq_active_order_per_vehicle"
    )
    indexed = set(index.document["partialFilterExpression"]["status"]["$in"])
    assert indexed == {status.value for status in ACTIVE_ORDER_STATUSES}
