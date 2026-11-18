import re
import unicodedata

_TRANSLITERATE = str.maketrans(
    {
        "ß": "ss",
        "æ": "ae",
        "Æ": "ae",
        "ø": "o",
        "Ø": "o",
        "œ": "oe",
        "Œ": "oe",
        "ł": "l",
        "Ł": "l",
        "đ": "d",
        "Đ": "d",
        "þ": "th",
        "Þ": "th",
    }
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_length: int = 200) -> str:
    """'Fauteuil lounge en chêne' -> 'fauteuil-lounge-en-chene'. Raises if nothing is left."""
    text = value.translate(_TRANSLITERATE)
    # Strip accents (é -> e); other symbols are kept so they become "-" separators.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    slug = _NON_ALNUM.sub("-", text.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    if not slug:
        raise ValueError(f"Cannot build a slug from {value!r}")
    return slug
