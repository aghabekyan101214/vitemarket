"""Password hashing (Argon2id) and admin access tokens (JWT, HS256)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with library-recommended parameters

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("dummy-password-for-timing")


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification (unknown user → no timing oracle)."""
    verify_password(_dummy_hash(), password)


# --- JWT ------------------------------------------------------------------------------

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "showcase-api"
JWT_AUDIENCE = "showcase-admin"


class InvalidToken(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_at: datetime
    expires_in: int


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: uuid.UUID
    issued_at: datetime


def create_access_token(
    user_id: uuid.UUID, *, secret: str, ttl_minutes: int, now: datetime | None = None
) -> AccessToken:
    now = now or datetime.now(UTC)
    expires_at = now + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": str(user_id),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return AccessToken(token=token, expires_at=expires_at, expires_in=ttl_minutes * 60)


def decode_access_token(token: str, *, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],  # never trust the header's alg
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss", "aud"]},
        )
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc
