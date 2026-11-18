"""Request context middleware (pure ASGI, no BaseHTTPMiddleware overhead).

- Assigns a request id (trusted inbound X-Request-ID, else CF-Ray, else random) and echoes it
  back in the X-Request-ID response header.
- Binds request_id into structlog contextvars so every log line carries it.
- Emits one structured access log line per request.
- Converts unhandled exceptions into a logged 500 problem+json response.
"""

import re
import time
import uuid

import structlog
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import problem_response
from app.core.logging import get_logger

log = get_logger("app.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def _pick_request_id(scope: Scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    for name in ("x-request-id", "cf-ray"):
        value: str | None = headers.get(name)
        if value and _REQUEST_ID_RE.match(value):
            return value
    return uuid.uuid4().hex


_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]
_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.extra_headers = [*_SECURITY_HEADERS, *([_HSTS] if hsts else [])]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _pick_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                headers.extend(self.extra_headers)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled_exception", method=scope["method"], path=scope["path"])
            if response_started:
                raise
            status_code = 500
            response = problem_response(
                Request(scope),
                status=500,
                code="internal_error",
                title="Internal server error",
            )
            await response(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            path = scope["path"]
            if not path.startswith("/health"):
                log.info(
                    "request",
                    method=scope["method"],
                    path=path,
                    status=status_code,
                    duration_ms=duration_ms,
                )
            structlog.contextvars.clear_contextvars()


class _BodyTooLarge(StarletteHTTPException):
    # An HTTPException so FastAPI's body parsing re-raises it (instead of turning it into a
    # 400) and the problem+json handler renders it; caught below if raised elsewhere.
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"Request bodies are limited to {limit} bytes")


class BodySizeLimitMiddleware:
    """Reject request bodies above a limit (413) before the app buffers them.

    Checks Content-Length up front and counts streamed bytes for chunked requests.
    Multipart uploads get their own, larger limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, max_multipart_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_multipart_bytes = max_multipart_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
        limit = (
            self.max_multipart_bytes
            if content_type.startswith("multipart/form-data")
            else self.max_bytes
        )
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = problem_response(
            Request(scope),
            status=413,
            code="payload_too_large",
            title="Payload too large",
            detail=f"Request bodies are limited to {limit} bytes",
        )
        await response(scope, receive, send)
