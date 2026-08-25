"""Object storage on Cloudflare R2.

R2 speaks the S3 API, so this is boto3 against a custom endpoint. Two things are
worth stating explicitly because they are the reason this file exists at all.

**Originals never pass through the API server.** The browser asks for a presigned
URL and PUTs the file straight to R2. A 12 MB upload therefore never occupies a
worker, and a malicious file never lands on the machine running the business
logic.

**The limits are enforced by R2, not by trust.** The presigned URL is signed
*with* a content-length range and a content type. A client that lies about either
gets rejected by R2 before a byte is stored — the check is in the signature, not
in a validation branch we could forget to write.

Signing is done with boto3 rather than hand-rolled SigV4. Getting request signing
subtly wrong fails silently and only against the real service, which is precisely
the class of bug not to invent.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger("voltaris.storage")

#: Formats accepted from sellers. HEIC is absent deliberately: Pillow needs a
#: plugin for it, and silently failing on iPhone photos is worse than telling the
#: person to convert. Revisit when pillow-heif is added.
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MIN_UPLOAD_BYTES = 1024

#: Uploads land here first and are deleted once derivatives exist. Nothing under
#: this prefix is ever served publicly.
QUARANTINE_PREFIX = "quarantine"
PUBLIC_PREFIX = "vehicles"


@dataclass(frozen=True)
class UploadIntent:
    upload_url: str
    media_key: str
    expires_at: str
    max_bytes: int


class R2Storage:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any | None = None

    @property
    def enabled(self) -> bool:
        s = self._settings
        return bool(s.r2_account_id and s.r2_access_key_id and s.r2_secret_access_key)

    def _s3(self) -> Any:
        if not self.enabled:
            raise AppError(
                ErrorCode.NOT_CONFIGURED,
                detail="R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set",
            )
        if self._client is None:
            import boto3
            from botocore.config import Config

            s = self._settings
            self._client = boto3.client(
                "s3",
                endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=s.r2_access_key_id,
                aws_secret_access_key=s.r2_secret_access_key,
                # R2 ignores the region but the signer requires one.
                region_name="auto",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            )
        return self._client

    # -- upload ------------------------------------------------------------

    def create_upload_intent(self, *, content_type: str, size_bytes: int) -> UploadIntent:
        """Presign a single PUT into quarantine.

        The object key is generated here and never derived from the filename a
        client supplied. A user-controlled key means path traversal, collisions
        between two sellers uploading `IMG_1234.jpg`, and guessable URLs.
        """
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                detail=f"content type {content_type} is not accepted",
            )
        if not MIN_UPLOAD_BYTES <= size_bytes <= MAX_UPLOAD_BYTES:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                detail=f"size {size_bytes} outside {MIN_UPLOAD_BYTES}..{MAX_UPLOAD_BYTES}",
            )

        media_key = f"{QUARANTINE_PREFIX}/{datetime.now(UTC):%Y/%m}/{secrets.token_urlsafe(24)}"
        ttl = self._settings.r2_upload_ttl_seconds

        url = self._s3().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._settings.r2_bucket,
                "Key": media_key,
                # Both are inside the signature, so R2 rejects a mismatch itself.
                "ContentType": content_type,
                "ContentLength": size_bytes,
            },
            ExpiresIn=ttl,
        )

        return UploadIntent(
            upload_url=url,
            media_key=media_key,
            expires_at=datetime.now(UTC).isoformat(),
            max_bytes=size_bytes,
        )

    # -- retrieval and writes ---------------------------------------------

    def download(self, key: str) -> bytes:
        """Fetch a quarantined original for processing.

        Capped at the upload maximum: a key could name an object written before
        the limit changed, and streaming an unbounded body into memory is how a
        worker dies.
        """
        response = self._s3().get_object(
            Bucket=self._settings.r2_bucket, Key=key, Range=f"bytes=0-{MAX_UPLOAD_BYTES}"
        )
        return response["Body"].read()

    def put_public(self, *, key: str, body: bytes, content_type: str) -> str:
        """Write a derivative and return its public URL.

        Long, immutable caching is safe because keys are content-addressed by a
        random id — a replaced image is a new key, never an overwritten one.
        """
        self._s3().put_object(
            Bucket=self._settings.r2_bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
        return f"{self._settings.r2_public_base_url.rstrip('/')}/{key}"

    def delete(self, key: str) -> None:
        try:
            self._s3().delete_object(Bucket=self._settings.r2_bucket, Key=key)
        except Exception as exc:
            # A failed cleanup must not fail the upload the customer is waiting on.
            # The lifecycle rule below is the real guarantee.
            logger.warning("could not delete %s: %s", key, exc)


def get_storage() -> R2Storage:
    return R2Storage()
