"""Rich-text sanitization (nh3/ammonia). Applied when content is validated, so stored and
served HTML can only contain this small, safe subset."""

import nh3

ALLOWED_TAGS = {
    "p", "br", "strong", "em", "b", "i", "u", "s", "a", "ul", "ol", "li",
    "h2", "h3", "h4", "blockquote", "code", "pre", "hr",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}


def sanitize_html(value: str) -> str:
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    ).strip()
