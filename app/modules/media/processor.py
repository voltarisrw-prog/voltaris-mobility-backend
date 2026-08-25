"""Turning a seller's photo into the four variants the frontend consumes.

Every image is **decoded and re-encoded**, never passed through. That single
decision does most of the security work here:

* It strips EXIF. A private seller photographs their car in their driveway, and
  the EXIF carries GPS coordinates — publishing the photo would publish their
  home address. This is the strongest reason for the whole pipeline, and it is a
  privacy failure rather than a security one, which is why it is easy to miss.
* It kills polyglots. A file that is simultaneously a valid JPEG and a valid
  archive or script does not survive being decoded to pixels and written back.
* It normalises. Sellers upload 4:3, 16:9, portrait, and rotated-by-EXIF photos;
  cards need one aspect ratio or the grid breaks.

The declared content type is never trusted. It is a client-supplied string, and
the actual format is read from the file's magic bytes.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageOps

from app.core.errors import AppError, ErrorCode

#: A 60 MB PNG can decompress to gigabytes of pixels. Pillow warns above its own
#: default; this refuses outright, well above any real photograph.
Image.MAX_IMAGE_PIXELS = 50_000_000

#: Leading bytes per accepted format. The client's `content_type` header is a
#: claim; this is the file.
MAGIC = {
    b"\xff\xd8\xff": "JPEG",
    b"\x89PNG\r\n\x1a\n": "PNG",
}

MIN_DIMENSION = 400
MAX_DIMENSION = 8000

#: The four sizes `VehicleImage` declares. Cropped to 4:3 so a grid of cards
#: never has a ragged edge; `gallery` keeps the full frame because the detail
#: page shows the whole photograph.
VARIANTS: dict[str, tuple[int, int, bool]] = {
    # name: (width, height, crop_to_fit)
    "thumb": (160, 120, True),
    "card": (640, 480, True),
    "detail": (1280, 960, True),
    "gallery": (1920, 1440, False),
}

WEBP_QUALITY = 78


@dataclass
class ProcessedImage:
    """Matches `VehicleImage` in the frontend, field for field."""

    thumb: str = ""
    card: str = ""
    detail: str = ""
    gallery: str = ""
    width: int = 0
    height: int = 0
    alt: str = ""
    blur_data_url: str = ""
    _rendered: dict[str, bytes] = field(default_factory=dict, repr=False)

    def as_document(self) -> dict[str, Any]:
        return {
            "thumb": self.thumb,
            "card": self.card,
            "detail": self.detail,
            "gallery": self.gallery,
            "width": self.width,
            "height": self.height,
            "alt": self.alt,
            "blur_data_url": self.blur_data_url,
        }


def detect_format(data: bytes) -> str:
    for magic, name in MAGIC.items():
        if data.startswith(magic):
            return name
    # WebP is RIFF....WEBP, so the marker is not at offset zero.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    raise AppError(
        ErrorCode.INVALID_REQUEST,
        detail=f"unrecognised image format, first bytes {data[:12]!r}",
    )


def render_variants(data: bytes, *, alt: str) -> ProcessedImage:
    """Validate, normalise, and derive. Returns bytes, uploads nothing."""
    detect_format(data)

    try:
        source = Image.open(io.BytesIO(data))
        source.load()
    except AppError:
        raise
    except Exception as exc:
        raise AppError(ErrorCode.INVALID_REQUEST, detail=f"could not decode image: {exc}") from exc

    # Apply the EXIF orientation flag, then discard it. Phones record "rotate 90"
    # rather than rotating pixels; ignoring it shows every portrait photo on its
    # side, and keeping the tag means the rotation applies twice after a resize.
    source = ImageOps.exif_transpose(source) or source

    if source.width < MIN_DIMENSION or source.height < MIN_DIMENSION:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            detail=f"{source.width}x{source.height} is below the {MIN_DIMENSION}px minimum",
        )
    if source.width > MAX_DIMENSION or source.height > MAX_DIMENSION:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            detail=f"{source.width}x{source.height} exceeds the {MAX_DIMENSION}px maximum",
        )

    # Flatten to RGB. A transparent PNG over the dark UI would show the page
    # through the car, and WebP encoding of P-mode images is unpredictable.
    if source.mode not in ("RGB", "L"):
        background = Image.new("RGB", source.size, (11, 18, 32))
        background.paste(source, mask=source.split()[-1] if "A" in source.mode else None)
        source = background
    elif source.mode == "L":
        source = source.convert("RGB")

    result = ProcessedImage(alt=alt)

    for name, (width, height, crop) in VARIANTS.items():
        if crop:
            variant = ImageOps.fit(source, (width, height), Image.LANCZOS, centering=(0.5, 0.5))
        else:
            variant = source.copy()
            variant.thumbnail((width, height), Image.LANCZOS)

        buffer = io.BytesIO()
        # No `exif=` argument, so Pillow writes none. That is the metadata strip:
        # it happens by omission, which is why it is called out here rather than
        # left to be inferred.
        variant.save(buffer, "WEBP", quality=WEBP_QUALITY, method=5)
        result._rendered[name] = buffer.getvalue()

        if name == "gallery":
            result.width, result.height = variant.size

    result.blur_data_url = _blur_placeholder(source)
    return result


def _blur_placeholder(source: Image.Image) -> str:
    """A ~200-byte data URI, inlined so the card never flashes empty.

    Twenty pixels wide: any larger and the base64 costs more than it saves, since
    it ships inside the HTML on every card in the grid.
    """
    import base64

    tiny = source.copy()
    tiny.thumbnail((20, 20), Image.LANCZOS)
    buffer = io.BytesIO()
    tiny.save(buffer, "WEBP", quality=35)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/webp;base64,{encoded}"
