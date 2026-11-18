from typing import Any

import pytest
from pydantic import ValidationError

from app.core.hostnames import InvalidHostname, normalize_hostname
from app.core.locale import InvalidLocale, fallback_chain, normalize_locale, pick_translation
from app.core.security import hash_password, password_needs_rehash, verify_password
from app.core.slugs import slugify
from app.modules.content.schemas import content_blocks_adapter
from app.modules.websites.schemas import ThemeTokens

# --- hostnames ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("www.example.com:443", "www.example.com"),
        ("shop.example.de.", "shop.example.de"),
        ("  example.fr  ", "example.fr"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("nordic-home-de.localhost", "nordic-home-de.localhost"),
    ],
)
def test_normalize_hostname(raw: str, expected: str) -> None:
    assert normalize_hostname(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "-bad.example.com",
        "bad-.example.com",
        "a..b.com",
        "127.0.0.1",
        "[::1]:8000",
        "example.com:abc",
        "under_score.example.com",
        "x" * 64 + ".com",
    ],
)
def test_normalize_hostname_rejects(raw: str) -> None:
    with pytest.raises(InvalidHostname):
        normalize_hostname(raw)


# --- locales --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("de", "de"), ("DE", "de"), ("pt_br", "pt-BR"), ("en-gb", "en-GB"), (" fr ", "fr")],
)
def test_normalize_locale(raw: str, expected: str) -> None:
    assert normalize_locale(raw) == expected


@pytest.mark.parametrize("raw", ["", "d", "deutsch", "de-DEU", "zh-Hant-TW", "1a"])
def test_normalize_locale_rejects(raw: str) -> None:
    with pytest.raises(InvalidLocale):
        normalize_locale(raw)


def test_fallback_chain() -> None:
    assert fallback_chain("EN", "de") == ["en", "de"]
    assert fallback_chain("de", "de") == ["de"]
    assert fallback_chain(None, "fr") == ["fr"]


def test_pick_translation() -> None:
    available = {"de": "Stuhl", "fr": "Chaise"}
    assert pick_translation(available, ["en", "de"]) == ("de", "Stuhl")
    assert pick_translation(available, ["fr", "de"]) == ("fr", "Chaise")
    assert pick_translation(available, ["en"]) is None


# --- slugs ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fauteuil lounge en chêne", "fauteuil-lounge-en-chene"),
        ("Dreisitzer-Sofa aus Wolle", "dreisitzer-sofa-aus-wolle"),
        ("Große Straße", "grosse-strasse"),
        ("  Ørsted & Søn  ", "orsted-son"),
        ("Table : 200\u00d795 cm!", "table-200-95-cm"),
        ("日本 chair", "chair"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_rejects_empty() -> None:
    with pytest.raises(ValueError, match="slug"):
        slugify("!!!")


# --- passwords ------------------------------------------------------------------------


def test_password_hashing() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong password!!")
    assert not verify_password("not-a-hash", "correct horse battery")
    assert not password_needs_rehash(hashed)


def test_password_minimum_length() -> None:
    with pytest.raises(ValueError, match="at least"):
        hash_password("short")


# --- JSON schemas ---------------------------------------------------------------------

_THEME: dict[str, Any] = {
    "colors": {
        "primary": "#123456",
        "primary_foreground": "#fff",
        "secondary": "#eee",
        "accent": "#c00",
        "background": "#ffffff",
        "foreground": "#111",
        "muted": "#777",
        "border": "#ddd",
    },
    "fonts": {"heading": {"family": "Inter"}, "body": {"family": "Inter"}},
}


def test_theme_tokens_valid() -> None:
    tokens = ThemeTokens.model_validate(_THEME)
    assert tokens.radius == "md"


@pytest.mark.parametrize(
    "patch",
    [{"colors": {**_THEME["colors"], "primary": "red"}}, {"radius": "huge"}, {"unknown": 1}],
)
def test_theme_tokens_invalid(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ThemeTokens.model_validate({**_THEME, **patch})


def test_content_blocks() -> None:
    blocks = content_blocks_adapter.validate_python(
        [
            {"type": "hero", "heading": "Hi"},
            {"type": "featured_products"},
            {"type": "cta", "heading": "Go", "link": {"label": "Shop", "href": "/products"}},
        ]
    )
    assert [b.type for b in blocks] == ["hero", "featured_products", "cta"]
    with pytest.raises(ValidationError):
        content_blocks_adapter.validate_python([{"type": "carousel"}])
    with pytest.raises(ValidationError):
        content_blocks_adapter.validate_python([{"type": "hero", "heading": "x", "extra": 1}])
