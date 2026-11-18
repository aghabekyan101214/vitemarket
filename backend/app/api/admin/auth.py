import math

from fastapi import APIRouter, Depends, Request

from app.api.admin.deps import CurrentAdmin
from app.api.deps import SettingsDep
from app.core.db import SessionDep
from app.core.errors import TooManyRequests
from app.core.ratelimit import RateLimiter
from app.core.security import create_access_token
from app.modules.auth.schemas import LoginIn, MeOut, TokenOut
from app.modules.auth.service import authenticate

router = APIRouter(prefix="/auth", tags=["admin: auth"])


def client_ip(request: Request) -> str:
    # Behind Cloudflare the edge sets CF-Connecting-IP (clients cannot spoof it there);
    # otherwise uvicorn's --proxy-headers already resolved request.client.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def login_rate_limit(request: Request, settings: SettingsDep) -> None:
    limiter: RateLimiter | None = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = request.app.state.login_limiter = RateLimiter(
            settings.login_rate_limit_per_minute, 60
        )
    retry_after = limiter.hit(client_ip(request))
    if retry_after is not None:
        raise TooManyRequests(
            "Too many login attempts; try again later",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


@router.post(
    "/login",
    response_model=TokenOut,
    summary="Exchange email + password for an access token",
    responses={
        401: {"description": "Invalid credentials (also returned while locked out)"},
        429: {"description": "Too many attempts from this IP"},
    },
    dependencies=[Depends(login_rate_limit)],
)
async def login(body: LoginIn, session: SessionDep, settings: SettingsDep) -> TokenOut:
    """5 consecutive failures lock the account for 15 minutes."""
    user = await authenticate(session, body.email, body.password)
    token = create_access_token(
        user.id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_minutes=settings.jwt_access_ttl_minutes,
    )
    return TokenOut(
        access_token=token.token, expires_in=token.expires_in, expires_at=token.expires_at
    )


@router.get("/me", response_model=MeOut, summary="The authenticated admin")
async def me(principal: CurrentAdmin) -> MeOut:
    return MeOut(
        id=principal.id,
        email=principal.email,
        role=principal.role.value,
        website_ids=sorted(principal.website_ids),
    )
