import io

import pytest
from PIL import Image

from app.modules.media.images import (
    PayloadTooLarge,
    UnsupportedMediaType,
    process_image,
)
from tests.images import exif_with_gps_and_rotation, make_image

MAX = 5 * 1024 * 1024


@pytest.mark.parametrize(
    ("fmt", "content_type", "ext"),
    [
        ("JPEG", "image/jpeg", "jpg"),
        ("PNG", "image/png", "png"),
        ("WEBP", "image/webp", "webp"),
        ("AVIF", "image/avif", "avif"),
    ],
)
def test_supported_formats(fmt: str, content_type: str, ext: str) -> None:
    result = process_image(make_image(fmt), max_bytes=MAX)
    assert (result.content_type, result.extension) == (content_type, ext)
    assert (result.width, result.height) == (64, 48)
    assert Image.open(io.BytesIO(result.data)).format == fmt


def test_exif_is_stripped_and_orientation_applied() -> None:
    data = make_image("JPEG", size=(64, 48), exif=exif_with_gps_and_rotation())
    assert b"SpyCam" in data
    result = process_image(data, max_bytes=MAX)
    assert b"SpyCam" not in result.data
    assert not Image.open(io.BytesIO(result.data)).getexif()
    assert (result.width, result.height) == (48, 64)  # rotated


def test_cmyk_jpeg_is_converted() -> None:
    result = process_image(make_image("JPEG", mode="CMYK"), max_bytes=MAX)
    assert Image.open(io.BytesIO(result.data)).mode == "RGB"


def test_too_large() -> None:
    with pytest.raises(PayloadTooLarge):
        process_image(make_image("PNG"), max_bytes=10)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_file"),
        (
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "unsupported_image",
        ),
        (make_image("GIF"), "unsupported_image"),
        (make_image("BMP"), "unsupported_image"),
        (make_image("JPEG")[:200], "corrupt_image"),
        (make_image("PNG", size=(9000, 9000)), "image_too_large"),
    ],
)
def test_rejected(data: bytes, code: str) -> None:
    with pytest.raises(UnsupportedMediaType) as exc:
        process_image(data, max_bytes=MAX)
    assert exc.value.code == code


def test_animated_rejected() -> None:
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:])
    with pytest.raises(UnsupportedMediaType) as exc:
        process_image(out.getvalue(), max_bytes=MAX)
    assert exc.value.code == "animated_image"
