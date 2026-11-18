"""Hostname normalization used for domain → website resolution.

Stored hostnames are always normalized: lowercase ASCII (IDN → punycode), no port,
no trailing dot. Every lookup normalizes the incoming Host the same way.
"""

import re

import idna

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_HOSTNAME_LENGTH = 253


class InvalidHostname(ValueError):
    pass


def normalize_hostname(value: str) -> str:
    host = value.strip()
    if not host:
        raise InvalidHostname("Hostname is empty")

    if host.startswith("["):  # IPv6 literal — never a website domain
        raise InvalidHostname(f"IP addresses are not valid website domains: {value!r}")
    if host.count(":") == 1:
        host, _, port = host.partition(":")
        if not port.isdigit():
            raise InvalidHostname(f"Invalid port in host: {value!r}")
    host = host.rstrip(".").lower()

    try:
        host = idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise InvalidHostname(f"Invalid hostname: {value!r}") from exc

    labels = host.split(".")
    if len(host) > MAX_HOSTNAME_LENGTH or len(labels) < 2:
        raise InvalidHostname(f"Invalid hostname: {value!r}")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise InvalidHostname(f"Invalid hostname: {value!r}")
    if all(label.isdigit() for label in labels):
        raise InvalidHostname(f"IP addresses are not valid website domains: {value!r}")
    return host
