from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, PaymentServiceDep
from app.core.errors import AppError, ErrorCode

router = APIRouter(tags=["payments"])


class CheckoutSessionResponse(BaseModel):
    payment_id: str
    redirect_url: str
    expires_at: str


class PaymentStateResponse(BaseModel):
    order_id: str
    state: str
    amount: int
    currency: str
    provider_reference: str | None
    updated_at: str


@router.post(
    "/orders/{order_id}/checkout-session",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_checkout_session(
    order_id: str, principal: CurrentUser, service: PaymentServiceDep
) -> CheckoutSessionResponse:
    result = await service.create_checkout_session(
        order_id=order_id, customer_id=principal.user_id, customer_email=principal.email
    )
    return CheckoutSessionResponse(
        payment_id=result["payment_id"],
        redirect_url=result["redirect_url"],
        expires_at=result["expires_at"],
    )


@router.get("/orders/{order_id}/payment", response_model=PaymentStateResponse)
async def get_payment_state(
    order_id: str, principal: CurrentUser, service: PaymentServiceDep
) -> PaymentStateResponse:
    return PaymentStateResponse(
        **await service.get_state_for_customer(order_id=order_id, customer_id=principal.user_id)
    )


@router.post("/webhooks/payments", include_in_schema=True)
async def payment_webhook(
    request: Request,
    service: PaymentServiceDep,
    signature: Annotated[str | None, Header(alias="X-Voltaris-Signature")] = None,
) -> dict[str, Any]:
    """The only endpoint that can move money.

    Unauthenticated by design — the provider has no session — but every request must
    carry a valid HMAC signature over the exact bytes received. The raw body is used
    for verification because re-serialising JSON changes whitespace and key order and
    would break the digest.
    """
    raw = await request.body()
    if signature is None:
        raise AppError(ErrorCode.WEBHOOK_SIGNATURE_INVALID, detail="missing signature header")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(ErrorCode.INVALID_REQUEST, detail="body is not JSON") from exc
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.INVALID_REQUEST, detail="body is not an object")

    return await service.handle_webhook(raw_body=raw, signature_header=signature, payload=payload)
