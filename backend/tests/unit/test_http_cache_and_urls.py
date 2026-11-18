import dataclasses
import uuid

import pytest

from app.core.http_cache import etag_matches, version_etag
from app.modules.public import urls
from app.modules.websites.resolution import SiteRef

# --- ETags ----------------------------------------------------------------------------


def test_version_etag_is_stable_and_param_order_independent() -> None:
    a = version_etag("site", 3, "products", {"locale": "de", "page": 1})
    b = version_etag("site", 3, "products", {"page": 1, "locale": "de"})
    assert a == b
    assert a.startswith('W/"site:3:products:')


@pytest.mark.parametrize(
    "other",
    [
        version_etag("site", 4, "products", {"locale": "de", "page": 1}),
        version_etag("site", 3, "home", {"locale": "de", "page": 1}),
        version_etag("site", 3, "products", {"locale": "en", "page": 1}),
        version_etag("other", 3, "products", {"locale": "de", "page": 1}),
    ],
)
def test_version_etag_changes(other: str) -> None:
    assert version_etag("site", 3, "products", {"locale": "de", "page": 1}) != other


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ("*", True),
        ('W/"abc"', True),
        ('"abc"', True),  # weak comparison ignores the W/ prefix
        ('W/"xyz", W/"abc"', True),
        ('W/"xyz"', False),
    ],
)
def test_etag_matches(header: str | None, expected: bool) -> None:
    assert etag_matches(header, 'W/"abc"') is expected


# --- URLs -----------------------------------------------------------------------------

SITE = SiteRef(
    id=uuid.uuid4(),
    key="nordic-home-de",
    name="Nordic Home",
    country_code="DE",
    default_locale="de",
    locales=("de", "en"),
    content_version=1,
    primary_host="nordic.example",
)


def test_paths() -> None:
    assert urls.home_path(SITE, "de") == "/"
    assert urls.home_path(SITE, "en") == "/en"
    assert urls.products_path(SITE, "de") == "/products"
    assert urls.product_path(SITE, "en", "chair") == "/en/products/chair"
    assert urls.absolute(SITE, "/en") == "https://nordic.example/en"


def test_absolute_without_primary_host_is_relative() -> None:
    site = dataclasses.replace(SITE, primary_host=None)
    assert urls.absolute(site, "/en") == "/en"


def test_hreflang() -> None:
    assert urls.hreflang(SITE, "de") == "de-DE"
    assert urls.hreflang(SITE, "pt-BR") == "pt-BR"
    assert urls.og_locale(SITE, "en") == "en_DE"


@pytest.mark.parametrize(
    ("locale", "href", "expected"),
    [
        ("de", "/products", "/products"),
        ("en", "/products", "/en/products"),
        ("en", "/", "/en"),
        ("en", "/en/products", "/en/products"),
        ("en", "/en", "/en"),
        ("en", "/english-guide", "/en/english-guide"),
        ("en", "https://example.com/x", "https://example.com/x"),
        ("en", "//cdn.example.com/x", "//cdn.example.com/x"),
        ("en", "#contact", "#contact"),
    ],
)
def test_localize_href(locale: str, href: str, expected: str) -> None:
    assert urls.localize_href(SITE, locale, href) == expected
