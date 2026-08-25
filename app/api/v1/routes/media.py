"""Upload endpoints. Authenticated: presigned URLs are capability tokens."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, MediaServiceDep

router = APIRouter(prefix="/media", tags=["media"])


class FileDescriptor(BaseModel):
    """What the client claims about a file.

    All three are claims, not facts. `content_type` and `size_bytes` are signed
    into the presigned URL so R2 rejects a mismatch itself; the real format is
    read from the magic bytes after upload. `filename` is used only to build the
    alt text — never the object key, which would be path traversal.
    """

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(gt=0, le=12 * 1024 * 1024)


class IntentsRequest(BaseModel):
    files: list[FileDescriptor] = Field(min_length=1, max_length=12)
    listing_reference: str | None = Field(default=None, max_length=64)


class Intent(BaseModel):
    upload_url: str
    media_key: str
    expires_at: str


@router.post("/intents", response_model=list[Intent], status_code=status.HTTP_201_CREATED)
async def create_intents(
    body: IntentsRequest, principal: CurrentUser, service: MediaServiceDep
) -> list[dict[str, Any]]:
    """Presign one PUT per file, straight into quarantine.

    The browser uploads to R2 directly, so a 12 MB file never occupies a worker
    here and never lands on the machine running the business logic.
    """
    return await service.create_intents(
        owner_id=principal.user_id,
        files=[f.model_dump() for f in body.files],
        listing_reference=body.listing_reference,
    )


class FinalizeRequest(BaseModel):
    media_keys: list[str] = Field(min_length=1, max_length=12)
    alt_prefix: str = Field(default="Vehicle photo", max_length=120)


class ImageOut(BaseModel):
    thumb: str
    card: str
    detail: str
    gallery: str
    width: int
    height: int
    alt: str
    blur_data_url: str


@router.post("/finalize", response_model=list[ImageOut])
async def finalize(
    body: FinalizeRequest, principal: CurrentUser, service: MediaServiceDep
) -> list[dict[str, Any]]:
    """Validate, re-encode, publish the four variants, delete the original.

    Re-encoding is what strips EXIF — including the GPS coordinates that would
    otherwise publish a private seller's home address.
    """
    return await service.finalize(
        media_keys=body.media_keys,
        owner_id=principal.user_id,
        alt_prefix=body.alt_prefix,
    )
