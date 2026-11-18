"""Public URLs for objects in the media bucket.

Object keys are immutable (never overwritten), so URLs are cacheable forever. Resizing is
done by the frontend's image loader (Cloudflare Image Transformations in production),
which needs only the base URL and the key.
"""

from app.core.config import Settings


def public_url(settings: Settings, key: str | None) -> str | None:
    if not key:
        return None
    return f"{settings.storage_public_base_url}/{key.lstrip('/')}"
