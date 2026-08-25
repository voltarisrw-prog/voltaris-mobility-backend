"""The money split. These are the tests that must never be allowed to go red."""

from __future__ import annotations

import pytest

from app.modules.commissions.service import calculate


def test_worked_example_from_the_brief():
    # Owner wants 25,000,000. Agency sells at 27,000,000. Commission is 2,000,000.
    breakdown = calculate(
        gross_sale=27_000_000,
        seller_expected_price=25_000_000,
        currency="RWF",
        commission_bps=700,
        payment_fee_bps=290,
    )
    assert breakdown.owner_settlement == 25_000_000
    assert breakdown.agency_commission == 2_000_000
    assert breakdown.payment_fees == 783_000
    assert breakdown.net_revenue == 1_217_000


def test_breakdown_always_sums_to_gross():
    breakdown = calculate(
        gross_sale=27_000_001,
        seller_expected_price=25_000_000,
        currency="RWF",
        commission_bps=100,
        payment_fee_bps=290,
    )
    assert breakdown.owner_settlement + breakdown.agency_commission == breakdown.gross_sale


def test_payment_fees_come_out_of_commission_not_the_seller():
    # The seller was promised a number. Provider fees are Voltaris's cost of doing
    # business and must never erode the settlement.
    breakdown = calculate(
        gross_sale=30_000_000,
        seller_expected_price=25_000_000,
        currency="RWF",
        commission_bps=500,
        payment_fee_bps=290,
    )
    assert breakdown.owner_settlement == 25_000_000
    assert breakdown.net_revenue == breakdown.agency_commission - breakdown.payment_fees


def test_refuses_to_pay_the_seller_more_than_the_customer_paid():
    with pytest.raises(ValueError, match="gross sale is only"):
        calculate(
            gross_sale=20_000_000,
            seller_expected_price=25_000_000,
            currency="RWF",
            commission_bps=700,
            payment_fee_bps=290,
        )


def test_refuses_a_spread_thinner_than_the_agreed_rate():
    # 27M gross at 800bps requires 2,160,000 of commission; the spread is 2,000,000.
    with pytest.raises(ValueError, match="below the agreed"):
        calculate(
            gross_sale=27_000_000,
            seller_expected_price=25_000_000,
            currency="RWF",
            commission_bps=800,
            payment_fee_bps=290,
        )


def test_integer_arithmetic_has_no_drift_over_many_sales():
    # A float implementation accumulates error here. Integers do not.
    total_gross = 0
    total_parts = 0
    for i in range(1_000):
        gross = 27_000_000 + i * 7
        breakdown = calculate(
            gross_sale=gross,
            seller_expected_price=25_000_000,
            currency="RWF",
            commission_bps=700,
            payment_fee_bps=290,
        )
        total_gross += breakdown.gross_sale
        total_parts += breakdown.owner_settlement + breakdown.agency_commission
    assert total_gross == total_parts


def test_rejects_non_positive_gross():
    with pytest.raises(ValueError):
        calculate(
            gross_sale=0,
            seller_expected_price=0,
            currency="RWF",
            commission_bps=0,
            payment_fee_bps=0,
        )
