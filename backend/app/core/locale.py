"""Locale handling.

Locales are BCP-47 language tags restricted to `ll` or `ll-RR` (e.g. `de`, `pt-BR`).
That covers country/language showcase sites; script subtags (`zh-Hant`) can be added later
by widening LOCALE_PATTERN (also used by the DB CHECK constraints).
"""

import re
from collections.abc import Iterable

LOCALE_PATTERN = r"^[a-z]{2,3}(-[A-Z]{2})?$"
_LOCALE_RE = re.compile(LOCALE_PATTERN)
_LOOSE_RE = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}))?$")


class InvalidLocale(ValueError):
    pass


def normalize_locale(value: str) -> str:
    """'DE' -> 'de', 'pt_br' -> 'pt-BR'. Raises InvalidLocale for anything else."""
    match = _LOOSE_RE.match(value.strip())
    if not match:
        raise InvalidLocale(f"Invalid locale: {value!r}")
    language, region = match.groups()
    return f"{language.lower()}-{region.upper()}" if region else language.lower()


def is_valid_locale(value: str) -> bool:
    return bool(_LOCALE_RE.match(value))


def fallback_chain(requested: str | None, default: str) -> list[str]:
    """Locales to try, in order: requested → website default (deduplicated)."""
    chain: list[str] = []
    if requested:
        chain.append(normalize_locale(requested))
    if default not in chain:
        chain.append(default)
    return chain


def pick_translation[T](available: dict[str, T], chain: Iterable[str]) -> tuple[str, T] | None:
    """First (locale, value) present in `available` following `chain`, else None."""
    for locale in chain:
        if locale in available:
            return locale, available[locale]
    return None
