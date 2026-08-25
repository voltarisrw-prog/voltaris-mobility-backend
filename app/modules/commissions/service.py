"""The money split. Integer minor units throughout — never floats.

A float cannot represent 0.1 exactly, so a settlement computed in floats drifts and
eventually fails to reconcile against the provider. Everything here is integer
arithmetic in the currency's minor unit, and the invariant

    gross = owner_settlement + agency_commission

is asserted before anything is written. Payment fees come out of Voltaris's own
commission, not out of the seller's settlement — the seller was promised a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class CommissionStatus(StrEnum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    REVERSED = "REVERSED"
    #: The customer paid, but the split could not be computed from the listing.
    #: Finance resolves these by hand. The money is never lost, only unallocated.
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class CommissionBreakdown:
    gross_sale: int
    owner_settlement: int
    agency_commission: int
    payment_fees: int
    net_revenue: int
    currency: str
    commission_bps: int

    def as_document(self, order_id: str) -> dict:
        return {
            "order_id": order_id,
            "gross_sale": self.gross_sale,
            "owner_settlement": self.owner_settlement,
            "agency_commission": self.agency_commission,
            "payment_fees": self.payment_fees,
            "net_revenue": self.net_revenue,
            "currency": self.currency,
            "commission_bps": self.commission_bps,
            "status": CommissionStatus.PENDING.value,
            "created_at": datetime.now(UTC),
        }


def calculate(
    *,
    gross_sale: int,
    seller_expected_price: int,
    currency: str,
    commission_bps: int,
    payment_fee_bps: int,
    payment_fee_fixed_minor: int = 0,
) -> CommissionBreakdown:
    """Derive the full financial breakdown for a completed sale.

    `seller_expected_price` is what the seller is owed and is taken literally. The
    commission is whatever is left over, so the arithmetic cannot silently short the
    seller when the agency price was set below their expectation — that case raises.
    """
    if gross_sale <= 0:
        raise ValueError("gross_sale must be positive")
    if seller_expected_price < 0:
        raise ValueError("seller_expected_price cannot be negative")
    if seller_expected_price > gross_sale:
        # Voltaris would be paying the seller more than the customer paid. That is a
        # pricing mistake upstream and must never be quietly absorbed.
        raise ValueError(
            f"seller expects {seller_expected_price} but gross sale is only {gross_sale}"
        )

    owner_settlement = seller_expected_price
    agency_commission = gross_sale - owner_settlement

    # A configured floor: if the spread is thinner than the agreed rate, the rate wins
    # and the discrepancy surfaces rather than eroding margin silently.
    minimum_commission = (gross_sale * commission_bps) // 10_000
    if agency_commission < minimum_commission:
        raise ValueError(
            f"spread {agency_commission} is below the agreed {commission_bps}bps "
            f"({minimum_commission}) on a gross sale of {gross_sale}"
        )

    payment_fees = (gross_sale * payment_fee_bps) // 10_000 + payment_fee_fixed_minor
    net_revenue = agency_commission - payment_fees

    assert owner_settlement + agency_commission == gross_sale, "breakdown must sum to gross"

    return CommissionBreakdown(
        gross_sale=gross_sale,
        owner_settlement=owner_settlement,
        agency_commission=agency_commission,
        payment_fees=payment_fees,
        net_revenue=net_revenue,
        currency=currency,
        commission_bps=commission_bps,
    )
