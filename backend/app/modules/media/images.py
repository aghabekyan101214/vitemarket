"""Upload validation and normalization for raster images (CPU-bound; run in a thread).

- Format is detected from the bytes, never from the filename or client content type.
- Allowed: JPEG, PNG, WebP, AVIF. No SVG (XSS risk), no animation.
- EXIF orientation is applied, then all metadata (EXIF/GPS/XMP/comments) is dropped by
  re-encoding; the ICC color profile is kept.
"""

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import AppError

MAX_PIXELS = 40_000_000  # 40 MP (e.g. 8000x5000): rejects decompression bombs early


class UnsupportedMediaType(AppError):
    status_code = 415
    code = "unsupported_media_type"
    title = "Unsupported media type"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    title = "Payload too large"


@dataclass(frozen=True, slots=True)
class ImageFormat:
    content_type: str
    extension: str


FORMATS: dict[str, ImageFormat] = {
    "JPEG": ImageFormat("image/jpeg", "jpg"),
    "PNG": ImageFormat("image/png", "png"),
    "WEBP": ImageFormat("image/webp", "webp"),
    "AVIF": ImageFormat("image/avif", "avif"),
}


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    data: bytes
    content_type: str
    extension: str
    width: int
    height: int


def _open(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            return Image.open(io.BytesIO(data), formats=list(FORMATS))
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise UnsupportedMediaType(
                "Not a supported image (JPEG, PNG, WebP or AVIF)", code="unsupported_image"
            ) from exc
        except (OSError, ValueError, SyntaxError) as exc:  # truncated/garbled headers
            raise UnsupportedMediaType("The image file is corrupt", code="corrupt_image") from exc


def process_image(data: bytes, *, max_bytes: int) -> ProcessedImage:
    if not data:
        raise UnsupportedMediaType("The file is empty", code="empty_file")
    if len(data) > max_bytes:
        raise PayloadTooLarge(f"Images must be at most {max_bytes // (1024 * 1024)} MB")

    probe = _open(data)
    fmt = FORMATS.get(probe.format or "")
    if fmt is None:
        raise UnsupportedMediaType("Unsupported image format", code="unsupported_image")
    if probe.width * probe.height > MAX_PIXELS:
        raise UnsupportedMediaType(
            f"Images may have at most {MAX_PIXELS // 1_000_000} megapixels", code="image_too_large"
        )
    if getattr(probe, "n_frames", 1) > 1:
        raise UnsupportedMediaType("Animated images are not supported", code="animated_image")
    try:
        probe.verify()  # structural check; the object is unusable afterwards
    except Exception as exc:
        raise UnsupportedMediaType("The image file is corrupt", code="corrupt_image") from exc

    image = _open(data)
    try:
        image.load()
    except Exception as exc:
        raise UnsupportedMediaType("The image file is corrupt", code="corrupt_image") from exc
    icc_profile = image.info.get("icc_profile")
    image = ImageOps.exif_transpose(image)  # bake orientation in before dropping EXIF

    out = io.BytesIO()
    params: dict[str, object] = {"icc_profile": icc_profile} if icc_profile else {}
    if probe.format == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(out, "JPEG", quality=90, optimize=True, progressive=True, **params)
    elif probe.format == "PNG":
        image.save(out, "PNG", optimize=True, **params)
    elif probe.format == "WEBP":
        image.save(out, "WEBP", quality=90, method=4, **params)
    else:
        image.save(out, "AVIF", quality=80, **params)

    return ProcessedImage(
        data=out.getvalue(),
        content_type=fmt.content_type,
        extension=fmt.extension,
        width=image.width,
        height=image.height,
    )
