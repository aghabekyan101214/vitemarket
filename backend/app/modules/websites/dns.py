"""DNS TXT lookup for domain ownership verification (`_showcase-verify.<hostname>`)."""

from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver

from app.core.errors import ServiceUnavailable

VERIFY_LABEL = "_showcase-verify"


def verification_record_name(hostname: str) -> str:
    return f"{VERIFY_LABEL}.{hostname}"


class TxtResolver(Protocol):
    async def txt(self, name: str) -> list[str]: ...


class DnsTxtResolver:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout = timeout_seconds

    async def txt(self, name: str) -> list[str]:
        try:
            answer = await dns.asyncresolver.resolve(name, "TXT", lifetime=self.timeout)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return []
        except dns.exception.Timeout as exc:
            raise ServiceUnavailable(f"DNS lookup for {name} timed out") from exc
        return [b"".join(r.strings).decode(errors="replace") for r in answer]
