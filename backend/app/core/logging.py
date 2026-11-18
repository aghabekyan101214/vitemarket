"""Structured logging via structlog.

Both structlog loggers and stdlib loggers (uvicorn, sqlalchemy, alembic) are rendered through
the same pipeline: JSON in production, colored console output locally. Request-scoped values
(request_id, website_id, ...) are bound with structlog.contextvars and appear on every line.
"""

import logging
import sys
from typing import Any

import structlog

from app.core.config import Settings


def _drop_color_message(_: Any, __: str, event_dict: Any) -> Any:
    # uvicorn passes a pre-colored duplicate of the message as an "extra"; drop it.
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(settings: Settings) -> None:
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
    ]

    renderer: Any
    if settings.log_format == "json":
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Uvicorn installs its own handlers; route everything through the root handler instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # We emit our own access log (with request_id + duration) from middleware.
    logging.getLogger("uvicorn.access").disabled = True
    # Chatty third-party DEBUG output (event loop, AWS SDK internals).
    for noisy in (
        "asyncio",
        "botocore",
        "boto3",
        "s3transfer",
        "urllib3",
        "PIL",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
