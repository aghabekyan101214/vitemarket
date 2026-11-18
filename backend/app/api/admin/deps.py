"""Admin API dependencies: bearer authentication, mutation context, If-Match parsing."""

import re
from typing import Annotated

import structlog
from fastapi import Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import SettingsDep
from app.core.db import SessionDep
from app.core.errors import PreconditionRequired, Unauthorized
from app.core.security import InvalidToken, decode_access_token
from app.modules.auth.principal import Principal
from app.modules.auth.service import load_principal
from app.modules.context import MutationContext
from app.modules.websites.dns import DnsTxtResolver, TxtResolver

_bearer = HTTPBearer(auto_error=False, description="Access token from POST /admin/v1/auth/login")


async def get_principal(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise Unauthorized("Missing bearer token", code="missing_token")
    try:
        claims = decode_access_token(
            credentials.credentials, secret=settings.jwt_secret.get_secret_value()
        )
    except InvalidToken:
        raise Unauthorized("Invalid or expired token", code="invalid_token") from None
    principal = await load_principal(session, claims.user_id, token_issued_at=claims.issued_at)
    if principal is None:
        raise Unauthorized("Invalid or expired token", code="invalid_token")
    structlog.contextvars.bind_contextvars(admin_user_id=str(principal.id))
    return principal


CurrentAdmin = Annotated[Principal, Depends(get_principal)]


def get_context(
    session: SessionDep, principal: CurrentAdmin, settings: SettingsDep
) -> MutationContext:
    return MutationContext(session=session, principal=principal, settings=settings)


Ctx = Annotated[MutationContext, Depends(get_context)]


def no_store(response: Response) -> None:
    """Admin responses are per-user and must never be cached by browsers or CDNs."""
    response.headers["Cache-Control"] = "no-store"


def get_txt_resolver(request: Request) -> TxtResolver:
    resolver: TxtResolver | None = getattr(request.app.state, "txt_resolver", None)
    return resolver or DnsTxtResolver()


TxtResolverDep = Annotated[TxtResolver, Depends(get_txt_resolver)]

_VERSION_TAG = re.compile(r'^\s*(?:W/)?"(\d+)"\s*$')


def parse_if_match(if_match: Annotated[str | None, Header()] = None) -> int | None:
    """`If-Match: "<version>"` → version (optimistic locking for theme/content)."""
    if if_match is None:
        return None
    match = _VERSION_TAG.match(if_match)
    if not match:
        raise PreconditionRequired(
            'If-Match must be a quoted version, e.g. If-Match: "3"', code="invalid_if_match"
        )
    return int(match.group(1))


IfMatch = Annotated[int | None, Depends(parse_if_match)]


def version_etag(version: int) -> str:
    return f'"{version}"'
