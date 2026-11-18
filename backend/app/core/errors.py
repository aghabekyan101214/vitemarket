"""Application errors rendered as RFC 9457 problem details (application/problem+json)."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"

log = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, client-facing errors. Raise from services; never return."""

    status_code: int = 400
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        if code:
            self.code = code
        self.extra = extra or {}
        self.headers = headers


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    title = "Not found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kwargs)


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    title = "Forbidden"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"
    title = "Validation failed"


class PreconditionFailed(AppError):
    status_code = 412
    code = "precondition_failed"
    title = "Precondition failed"


class PreconditionRequired(AppError):
    status_code = 428
    code = "precondition_required"
    title = "Precondition required"


class TooManyRequests(AppError):
    status_code = 429
    code = "too_many_requests"
    title = "Too many requests"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    title = "Service unavailable"


_STATUS_TITLES = {
    400: ("bad_request", "Bad request"),
    401: ("unauthorized", "Authentication required"),
    403: ("forbidden", "Forbidden"),
    404: ("not_found", "Not found"),
    405: ("method_not_allowed", "Method not allowed"),
    409: ("conflict", "Conflict"),
    412: ("precondition_failed", "Precondition failed"),
    413: ("payload_too_large", "Payload too large"),
    428: ("precondition_required", "Precondition required"),
    415: ("unsupported_media_type", "Unsupported media type"),
    422: ("validation_failed", "Validation failed"),
    429: ("too_many_requests", "Too many requests"),
    500: ("internal_error", "Internal server error"),
    503: ("service_unavailable", "Service unavailable"),
}


def problem_response(
    request: Request | None,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    if request is not None:
        body["instance"] = request.url.path
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            body["request_id"] = request_id
    if extra:
        body.update(extra)
    return JSONResponse(
        jsonable_encoder(body),
        status_code=status,
        headers=headers,
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return problem_response(
        request,
        status=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        extra=exc.extra,
        headers=exc.headers,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, title = _STATUS_TITLES.get(exc.status_code, ("http_error", "HTTP error"))
    detail = (
        exc.detail if isinstance(exc.detail, str) and exc.detail.lower() != title.lower() else None
    )
    return problem_response(
        request,
        status=exc.status_code,
        code=code,
        title=title,
        detail=detail,
        headers=getattr(exc, "headers", None),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation_failed",
        title="Validation failed",
        detail="The request is invalid.",
        extra={"errors": errors},
    )


async def _integrity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, IntegrityError)
    constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
    log.info("integrity_error", constraint=constraint)
    return problem_response(
        request,
        status=409,
        code="conflict",
        title="Conflict",
        detail="The request conflicts with existing data.",
        extra={"constraint": constraint} if constraint else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(IntegrityError, _integrity_error_handler)
    # Unhandled exceptions are caught by RequestContextMiddleware (logged + 500 problem).
