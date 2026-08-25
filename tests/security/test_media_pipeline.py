"""The image pipeline exists to make a seller's photo safe to publish.

Each test below corresponds to something that would otherwise reach the public
site: a home address in EXIF, a payload hidden in a JPEG, a decompression bomb,
or a file that is not an image at all.
"""

from __future__ import annotations

import io

import piexif
import pytest
from PIL import Image

from app.core.errors import AppError, ErrorCode
from app.modules.media.processor import (
    MAX_DIMENSION,
    MIN_DIMENSION,
    VARIANTS,
    detect_format,
    render_variants,
)


def photo(width: int = 1600, height: int = 1200, fmt: str = "JPEG", mode: str = "RGB") -> bytes:
    image = Image.new(mode, (width, height), (120, 140, 160))
    # A little structure, so resizing is doing real work rather than smoothing flat colour.
    for x in range(0, width, 40):
        for y in range(0, height, 40):
            image.putpixel((x, y), (250, 250, 250) if mode == "RGB" else 250)
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


def photo_with_gps() -> bytes:
    """A JPEG carrying GPS coordinates, exactly as a phone would write it."""
    exif = {
        "0th": {piexif.ImageIFD.Make: b"Apple", piexif.ImageIFD.Model: b"iPhone 15"},
        "Exif": {},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"S",
            piexif.GPSIFD.GPSLatitude: ((1, 1), (57, 1), (0, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: ((30, 1), (3, 1), (0, 1)),
        },
        "1st": {},
        "thumbnail": None,
    }
    buffer = io.BytesIO()
    Image.new("RGB", (1600, 1200), (100, 110, 120)).save(buffer, "JPEG", exif=piexif.dump(exif))
    return buffer.getvalue()


# -- the privacy case ---------------------------------------------------------


def test_gps_coordinates_are_stripped():
    """The reason this pipeline exists.

    A seller photographs their car in their driveway. The EXIF carries their home
    coordinates. Publishing the file unchanged publishes their address.
    """
    original = photo_with_gps()
    assert piexif.load(original)["GPS"], "fixture should carry GPS"

    result = render_variants(original, alt="test")

    for name in VARIANTS:
        rendered = result._rendered[name]
        # WebP output from Pillow with no exif= argument carries no metadata.
        assert b"GPS" not in rendered
        assert b"Apple" not in rendered
        assert b"iPhone" not in rendered
        reopened = Image.open(io.BytesIO(rendered))
        assert not reopened.getexif(), f"{name} still carries EXIF"


def test_camera_make_and_model_do_not_survive():
    result = render_variants(photo_with_gps(), alt="test")
    for rendered in result._rendered.values():
        assert b"iPhone 15" not in rendered


# -- format validation --------------------------------------------------------


def test_a_file_that_is_not_an_image_is_rejected():
    with pytest.raises(AppError) as exc:
        render_variants(b"#!/bin/sh\nrm -rf /\n", alt="test")
    assert exc.value.code is ErrorCode.INVALID_REQUEST


def test_a_declared_content_type_cannot_launder_a_non_image():
    """`content_type` is a client claim. The magic bytes are the file."""
    with pytest.raises(AppError):
        detect_format(b"GIF89a" + b"\x00" * 100)  # real GIF, not on the accept list
    with pytest.raises(AppError):
        detect_format(b"%PDF-1.7\n")


def test_a_polyglot_does_not_survive_re_encoding():
    """A file that is a valid JPEG with an archive appended.

    Re-encoding decodes to pixels and writes back, so anything riding along after
    the image data is simply not carried over.
    """
    payload = b"PK\x03\x04" + b"malicious archive contents" * 50
    polyglot = photo() + payload

    result = render_variants(polyglot, alt="test")
    for rendered in result._rendered.values():
        assert b"malicious archive contents" not in rendered
        assert not rendered.startswith(b"PK")


def test_accepted_formats_are_recognised():
    assert detect_format(photo(fmt="JPEG")) == "JPEG"
    assert detect_format(photo(fmt="PNG")) == "PNG"
    assert detect_format(photo(fmt="WEBP")) == "WEBP"


# -- dimension limits ---------------------------------------------------------


def test_a_thumbnail_sized_upload_is_rejected():
    with pytest.raises(AppError) as exc:
        render_variants(photo(width=200, height=150), alt="test")
    assert "below" in str(exc.value.detail)


def test_an_absurdly_large_image_is_rejected():
    """Guards the worker, not the disk: decoding this is the expensive part."""
    with pytest.raises(AppError) as exc:
        render_variants(photo(width=MAX_DIMENSION + 100, height=600), alt="test")
    assert "exceeds" in str(exc.value.detail)


def test_the_smallest_permitted_image_is_accepted():
    result = render_variants(photo(width=MIN_DIMENSION, height=MIN_DIMENSION), alt="test")
    assert result.width > 0


# -- output shape -------------------------------------------------------------


def test_all_four_variants_are_produced_as_webp():
    result = render_variants(photo(), alt="2023 BYD Atto 3")
    assert set(result._rendered) == set(VARIANTS)
    for name, rendered in result._rendered.items():
        assert rendered[:4] == b"RIFF" and rendered[8:12] == b"WEBP", f"{name} is not WebP"


def test_cropped_variants_share_one_aspect_ratio():
    """Cards sit in a grid; a ragged edge is a broken grid."""
    result = render_variants(photo(width=1600, height=900), alt="test")
    for name in ("thumb", "card", "detail"):
        image = Image.open(io.BytesIO(result._rendered[name]))
        assert abs(image.width / image.height - 4 / 3) < 0.01, f"{name} is {image.size}"


def test_gallery_keeps_the_whole_frame():
    """The detail page shows the photograph, not a crop of it."""
    result = render_variants(photo(width=1600, height=900), alt="test")
    gallery = Image.open(io.BytesIO(result._rendered["gallery"]))
    assert abs(gallery.width / gallery.height - 16 / 9) < 0.01


def test_variants_get_progressively_smaller():
    result = render_variants(photo(), alt="test")
    sizes = [len(result._rendered[n]) for n in ("thumb", "card", "detail")]
    assert sizes == sorted(sizes), f"expected ascending, got {sizes}"


def test_blur_placeholder_is_tiny_enough_to_inline():
    """It ships in the HTML for every card in the grid."""
    result = render_variants(photo(), alt="test")
    assert result.blur_data_url.startswith("data:image/webp;base64,")
    assert len(result.blur_data_url) < 600, len(result.blur_data_url)


def test_transparency_is_flattened_not_punched_through():
    """A transparent PNG would otherwise show the page through the car."""
    image = Image.new("RGBA", (1200, 900), (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    result = render_variants(buffer.getvalue(), alt="test")
    rendered = Image.open(io.BytesIO(result._rendered["card"]))
    assert rendered.mode in ("RGB", "RGBX"), rendered.mode


def test_alt_text_is_carried_through():
    result = render_variants(photo(), alt="2023 BYD Atto 3 — photo 1")
    assert result.as_document()["alt"] == "2023 BYD Atto 3 — photo 1"


def test_document_matches_the_frontend_image_type():
    document = render_variants(photo(), alt="test").as_document()
    assert set(document) == {
        "thumb",
        "card",
        "detail",
        "gallery",
        "width",
        "height",
        "alt",
        "blur_data_url",
    }
