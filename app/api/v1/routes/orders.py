from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, OrderServiceDep
from app.modules.orders.models import OrderKind

router = APIRouter(prefix="/orders", tags=["orders"])


class CreateOrderRequest(BaseModel):
    """No amount, no price, no currency, no discount.

    The client states what it wants to buy; the server decides what that costs. There
    is deliberately no field here that could be tampered with to change the total.
    """

    vehicle_id: str = Field(min_length=1, max_length=64)
    kind: OrderKind = OrderKind.PURCHASE
    rental_days: int | None = Field(default=None, ge=1, le=365)


class OrderVehicle(BaseModel):
    id: str
    slug: str
    title: str


class OrderLineOut(BaseModel):
    label: str
    amount: int


class OrderResponse(BaseModel):
    id: str
    reference: str
    vehicle: OrderVehicle
    kind: str
    status: str
    lines: list[OrderLineOut]
    total: int
    currency: str
    created_at: str


class OrderPage(BaseModel):
    items: list[OrderResponse]
    next_cursor: str | None = None


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    body: CreateOrderRequest,
    principal: CurrentUser,
    service: OrderServiceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> OrderResponse:
    result = await service.create_order(
        customer_id=principal.user_id,
        vehicle_id=body.vehicle_id,
        kind=body.kind,
        rental_days=body.rental_days,
        idempotency_key=idempotency_key,
    )
    return OrderResponse(**result)


@router.get("", response_model=OrderPage)
async def list_orders(
    principal: CurrentUser,
    service: OrderServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> OrderPage:
    items, next_cursor = await service.list_for_customer(
        customer_id=principal.user_id, limit=limit, cursor=cursor
    )
    return OrderPage(items=[OrderResponse(**item) for item in items], next_cursor=next_cursor)


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str, principal: CurrentUser, service: OrderServiceDep
) -> OrderResponse:
    # The customer id is part of the query, so another customer's order is a 404
    # rather than a 403 — no confirmation that the id exists.
    return OrderResponse(
        **await service.get_for_customer(order_id=order_id, customer_id=principal.user_id)
    )
