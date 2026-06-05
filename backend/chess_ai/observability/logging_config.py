"""Structured logging for the blitzy-chess backend.

Configures structlog to emit one JSON object per line on stdout and routes the
standard library ``logging`` module through the same formatter, so application
logs and third-party logs (uvicorn, fastapi, python-chess) share one structured
stream. Every line carries the active correlation id and any per-game or
per-connection context bound by the API layer.

``chess_ai.app`` calls :func:`configure_logging` once at startup. This module
configures nothing at import time and is never imported by ``chess_ai.engine``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
from uuid import uuid4

import structlog

try:
    from asgi_correlation_id.context import correlation_id
except ImportError:  # pragma: no cover - optional dependency in minimal envs
    correlation_id = None


__all__ = [
    "configure_logging",
    "get_logger",
    "bind_log_context",
    "clear_log_context",
    "bind_correlation_id",
    "add_correlation_id",
    "new_correlation_id",
]


# ---------------------------------------------------------------------------
# Correlation-id processor
# ---------------------------------------------------------------------------
def add_correlation_id(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach the active correlation id to a log event when one is set.

    The id is read from the ``asgi-correlation-id`` ContextVar, set per HTTP
    request by ``CorrelationIdMiddleware`` and per WebSocket connection by
    :func:`bind_correlation_id`. The processor is a pass-through when the
    package is absent.

    Args:
        logger: The wrapped logger; required by the processor protocol.
        method_name: The log method name; required by the processor protocol.
        event_dict: The structlog event dictionary to enrich.

    Returns:
        The event dictionary, with ``correlation_id`` added when available.
    """
    if correlation_id is not None:
        try:
            cid = correlation_id.get()
        except Exception:
            cid = None
        if cid:
            event_dict["correlation_id"] = cid
    return event_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def configure_logging(log_level: str | None = None, json_logs: bool | None = None) -> None:
    """Configure structlog and the stdlib logging bridge for the whole app.

    Call once at startup. Calling again is safe: the root logger's handlers are
    cleared and replaced, so repeated calls do not duplicate output.

    Output is one JSON object per line on stdout by default. Pass
    ``json_logs=False`` or set ``LOG_JSON=0`` to use the console renderer. The
    level comes from ``log_level``, else ``LOG_LEVEL``, else ``INFO``.

    HTTP requests get a correlation id from ``CorrelationIdMiddleware``.
    WebSocket connections are not covered by that middleware and must call
    :func:`bind_correlation_id` to set one.

    Args:
        log_level: Log level name such as ``"DEBUG"``; overrides ``LOG_LEVEL``.
        json_logs: Force JSON (``True``) or console (``False``) output;
            overrides ``LOG_JSON``.
    """
    level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    if json_logs is None:
        use_json = os.environ.get("LOG_JSON", "1") != "0"
    else:
        use_json = json_logs

    # Processor chain shared by structlog-native loggers and the stdlib bridge.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_correlation_id,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    renderer = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()  # idempotent: replace handlers on every (re)configure
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's loggers through the single root handler exactly once.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        noisy_logger = logging.getLogger(noisy)
        noisy_logger.handlers.clear()
        noisy_logger.propagate = True


# ---------------------------------------------------------------------------
# Logger accessor and context helpers
# ---------------------------------------------------------------------------
def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger bound to the shared configuration.

    Args:
        name: Optional logger name, surfaced as the ``logger`` field.

    Returns:
        A bound structlog logger backed by the stdlib logger factory.
    """
    return structlog.get_logger(name)


def bind_log_context(**kwargs: Any) -> None:
    """Bind key/value context onto the current execution context.

    Bound fields such as ``game_id``, ``room_code``, ``connection_id`` and
    ``mode`` appear on every subsequent log line in the same context, including
    across awaited calls within the same task.

    Args:
        **kwargs: Fields to merge into the logging context.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_log_context() -> None:
    """Clear all context bound by :func:`bind_log_context`.

    Call on connection close or game end so context does not leak across tasks.
    """
    structlog.contextvars.clear_contextvars()


def bind_correlation_id(value: str) -> None:
    """Set the correlation id for the current context.

    WebSocket handlers call this once at connection start because they are not
    covered by ``CorrelationIdMiddleware``. This is a no-op when
    ``asgi-correlation-id`` is not installed.

    Args:
        value: Correlation id to attach to subsequent log lines.
    """
    if correlation_id is not None:
        correlation_id.set(value)


def new_correlation_id() -> str:
    """Return a fresh correlation id for use with :func:`bind_correlation_id`.

    Returns:
        A 32-character hexadecimal id.
    """
    return uuid4().hex
