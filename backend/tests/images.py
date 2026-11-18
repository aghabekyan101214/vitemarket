"""Generate test images in memory."""

import io

from PIL import Image


def make_image(
    fmt: str = "JPEG",
    size: tuple[int, int] = (64, 48),
    mode: str = "RGB",
    exif: Image.Exif | None = None,
    **save: object,
) -> bytes:
    image = Image.new(mode, size, "red" if mode != "CMYK" else (0, 255, 255, 0))
    out = io.BytesIO()
    if exif is not None:
        save["exif"] = exif
    image.save(out, fmt, **save)
    return out.getvalue()


def exif_with_gps_and_rotation() -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW
    exif[0x010F] = "SpyCam"  # Make
    exif[0x8825] = {1: "N", 2: (52.0, 31.0, 0.0)}  # GPSInfo
    return exif
