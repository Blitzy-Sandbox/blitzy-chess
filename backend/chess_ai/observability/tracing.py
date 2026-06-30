"""OpenTelemetry tracing for the blitzy-chess backend.

Builds a ``TracerProvider`` so spans flow across the WebSocket-handler to engine
boundary. ``chess_ai.app`` calls :func:`setup_tracing` at startup, optionally
wraps the app with :func:`instrument_fastapi_app`, and calls
:func:`shutdown_tracing` from the lifespan shutdown. The API layer opens spans
around the engine call with :func:`get_tracer` or :func:`span`. The pure
``chess_ai.engine`` package never imports this module.

Export is opt-in. With no OTLP endpoint configured the provider runs without a
network exporter, so the app starts and runs normally with no collector
present. The OTLP exporter and the FastAPI instrumentor are imported lazily, so
importing this module succeeds even in minimal environments. This module
configures nothing at import time.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


__all__ = [
    "setup_tracing",
    "instrument_fastapi_app",
    "get_tracer",
    "span",
    "shutdown_tracing",
]


logger = logging.getLogger(__name__)

# Fallback service name.
DEFAULT_SERVICE_NAME = "blitzy-chess"

# Module-global provider state.
_provider: TracerProvider | None = None
_configured: bool = False


# ---------------------------------------------------------------------------
# Provider setup
# ---------------------------------------------------------------------------
def setup_tracing(
    service_name: str | None = None,
    otlp_endpoint: str | None = None,
) -> TracerProvider:
    """Configure the global tracer provider and return it.

    Call once at startup. The provider is a process-lifetime singleton: calling
    again (including after :func:`shutdown_tracing`) returns the
    already-registered provider without adding another span processor or trying
    to replace the global provider, which OpenTelemetry does not allow. An OTLP
    exporter is attached only when an endpoint is configured, either via
    ``otlp_endpoint`` or the ``OTEL_EXPORTER_OTLP_ENDPOINT`` environment
    variable. With no endpoint and
    ``OTEL_CONSOLE_EXPORT=1``, a console exporter is attached for local
    inspection; otherwise the provider runs without an exporter. Exporter
    construction failures are logged and swallowed, so this function does not
    raise for environmental reasons.

    Args:
        service_name: Service name for the resource; overrides
            ``OTEL_SERVICE_NAME``. Defaults to ``"blitzy-chess"``.
        otlp_endpoint: OTLP collector endpoint; overrides
            ``OTEL_EXPORTER_OTLP_ENDPOINT``.

    Returns:
        The configured :class:`TracerProvider`.
    """
    global _provider, _configured

    if _configured and _provider is not None:
        return _provider

    # OpenTelemetry registers the global tracer provider once per process and
    # refuses to replace it afterward. If a concrete SDK provider is already
    # registered (for example by an earlier setup in this process), adopt it so
    # the returned provider stays identical to trace.get_tracer_provider().
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        _provider = existing
        _configured = True
        return existing

    resolved_name = service_name or os.environ.get("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME
    resource = Resource.create({SERVICE_NAME: resolved_name})
    provider = TracerProvider(resource=resource)

    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        processor = _build_otlp_processor(endpoint)
        if processor is not None:
            provider.add_span_processor(processor)
    elif os.environ.get("OTEL_CONSOLE_EXPORT") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    atexit.register(_atexit_shutdown)
    _provider = provider
    _configured = True
    return provider


def _build_otlp_processor(endpoint: str) -> BatchSpanProcessor | None:
    """Build a batch processor around an OTLP exporter for ``endpoint``.

    The gRPC exporter is tried first and the HTTP exporter second, both imported
    lazily so a missing exporter package or transport never breaks module
    import. Any failure is logged and reported as ``None``.

    Args:
        endpoint: OTLP collector endpoint.

    Returns:
        A :class:`BatchSpanProcessor`, or ``None`` if no exporter could be
        constructed.
    """
    try:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        return BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    except Exception as exc:  # pragma: no cover - optional dependency / transport
        logger.warning("OTLP exporter unavailable; tracing spans will not be exported: %s", exc)
        return None


# ---------------------------------------------------------------------------
# FastAPI instrumentation
# ---------------------------------------------------------------------------
def instrument_fastapi_app(app: FastAPI) -> None:
    """Instrument a FastAPI app for automatic request and route spans.

    A safe single call for ``app.py`` to make after the app is created. The
    instrumentor is imported lazily and every failure is logged and swallowed,
    so a missing instrumentation package or a repeated call never crashes the
    app.

    Args:
        app: The FastAPI application to instrument.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError as exc:  # pragma: no cover - optional dependency
        logger.warning("FastAPI instrumentation unavailable: %s", exc)
        return

    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover - already instrumented / defensive
        logger.warning("FastAPI instrumentation skipped: %s", exc)


# ---------------------------------------------------------------------------
# Tracer access
# ---------------------------------------------------------------------------
def get_tracer(name: str | None = None) -> trace.Tracer:
    """Return a tracer from the global provider.

    The API layer uses this to open a span around the engine call, for example
    ``with get_tracer(__name__).start_as_current_span("engine.search"): ...``.

    Args:
        name: Instrumentation scope name; defaults to ``"chess_ai"``.

    Returns:
        A :class:`opentelemetry.trace.Tracer`.
    """
    return trace.get_tracer(name or "chess_ai")


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open a span, set attributes, and record exceptions.

    Records the exception and sets an error status when the body raises, then
    re-raises. The active span is yielded so callers can add more attributes.

    Args:
        name: Span name.
        **attributes: Attributes set on the span at entry.

    Yields:
        The active :class:`opentelemetry.trace.Span`.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current_span:
        for key, value in attributes.items():
            current_span.set_attribute(key, value)
        try:
            yield current_span
        except Exception as exc:
            current_span.record_exception(exc)
            current_span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def shutdown_tracing() -> None:
    """Flush the global tracer provider's pending spans.

    Called from the application lifespan shutdown. Flushes the batch processor
    so buffered spans are exported. It deliberately does NOT reset module state
    or shut the provider down for good: OpenTelemetry's global tracer provider
    is set once per process and cannot be replaced, so the provider is kept as
    the process-lifetime singleton and a later :func:`setup_tracing` returns the
    same provider. Final teardown runs once at interpreter exit via the
    registered :func:`_atexit_shutdown`. Safe to call when
    :func:`setup_tracing` was never called.
    """
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Tracing flush failed: %s", exc)


def _atexit_shutdown() -> None:
    """Shut down the provider once at interpreter exit to release resources.

    Registered by :func:`setup_tracing` for a provider this module created and
    registered. Best-effort: any failure during interpreter shutdown is
    suppressed.
    """
    if _provider is not None:
        with contextlib.suppress(Exception):
            _provider.shutdown()
