"""Enquiries and test drives. Open to anonymous visitors by design — requiring an
account before someone can ask a question loses the lead."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import LeadServiceDep, OptionalUser

router = APIRouter(tags=["leads"])


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


class InquiryRequest(BaseModel):
    """Covers both shapes the frontend sends.

    A vehicle enquiry carries `vehicle_id` and `preferred_channel`; the homepage
    form carries `topic` and `source` and no vehicle. One endpoint, because the
    record is the same and the routing difference is the backend's business.
    """

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=20)
    message: str = Field(min_length=10, max_length=1500)
    vehicle_id: str | None = Field(default=None, max_length=64)
    preferred_channel: str | None = Field(default=None, max_length=20)
    topic: str | None = Field(default=None, max_length=40)
    source: str = Field(default="direct", max_length=60)
    #: Honeypot. Hidden and out of the tab order, so only a bot fills it.
    company_website: str | None = Field(default=None, max_length=200)


class InquiryResponse(BaseModel):
    reference: str
    status: str


@router.post("/inquiries", response_model=InquiryResponse, status_code=status.HTTP_201_CREATED)
async def create_inquiry(
    body: InquiryRequest,
    service: LeadServiceDep,
    request: Request,
    principal: OptionalUser,
) -> InquiryResponse:
    result = await service.create_inquiry(
        full_name=body.full_name,
        email=str(body.email),
        phone=body.phone,
        message=body.message,
        vehicle_id=body.vehicle_id,
        preferred_channel=body.preferred_channel,
        topic=body.topic,
        source=body.source,
        customer_id=principal.user_id if principal else None,
        honeypot=body.company_website,
        ip=client_ip(request),
    )
    return InquiryResponse(**result)


class TestDriveRequest(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=20)
    preferred_date: str = Field(min_length=8, max_length=32)
    preferred_time_slot: str = Field(pattern="^(morning|afternoon|evening)$")
    location_slug: str = Field(min_length=1, max_length=60)
    notes: str | None = Field(default=None, max_length=600)


class TestDriveResponse(BaseModel):
    reference: str
    status: str
    scheduled_for: str | None = None


@router.post("/test-drives", response_model=TestDriveResponse, status_code=status.HTTP_201_CREATED)
async def request_test_drive(
    body: TestDriveRequest,
    service: LeadServiceDep,
    request: Request,
    principal: OptionalUser,
) -> TestDriveResponse:
    result = await service.request_test_drive(
        vehicle_id=body.vehicle_id,
        full_name=body.full_name,
        email=str(body.email),
        phone=body.phone,
        preferred_date=body.preferred_date,
        preferred_time_slot=body.preferred_time_slot,
        location_slug=body.location_slug,
        notes=body.notes,
        customer_id=principal.user_id if principal else None,
        ip=client_ip(request),
    )
    return TestDriveResponse(**result)


@router.get("/test-drives/{reference}", response_model=TestDriveResponse)
async def get_test_drive(reference: str, service: LeadServiceDep) -> TestDriveResponse:
    """Public status lookup. Returns status and time only — never the customer's
    details, since the reference is the only thing guarding it."""
    result: dict[str, Any] = await service.get_test_drive(reference)
    return TestDriveResponse(**result)
