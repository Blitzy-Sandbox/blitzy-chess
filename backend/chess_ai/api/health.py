"""REST health and readiness endpoints for the blitzy-chess backend.

Exposes a module-level ``router`` that ``chess_ai.app`` includes at the app
root. ``GET /health`` is a dependency-free liveness check that returns 200 while
the process is alive. ``GET /health/ready`` and its ``GET /ready`` alias report
startup completion and which optional resources are loaded, returning 503 until
the application is ready.

This module handles no chess moves. Readiness reads ``request.app.state``
defensively, so a minimal or test application without a configured lifespan
still answers every route.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from chess_ai.observability.logging_config import get_logger

logger = get_logger(__name__)

__all__ = ["router"]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
# No prefix: the paths below are absolute at the application root, matching the
# routes ``chess_ai.app`` registers and the readiness probe ops references.
router = APIRouter(tags=["health"])


# ---------------------------------------------------------------------------
# Readiness payload
# ---------------------------------------------------------------------------
def _readiness_payload(request: Request) -> tuple[dict, int]:
    """Compute the readiness body and its HTTP status code.

    Reads application state defensively: a missing attribute degrades to its
    documented default instead of raising. The opening book and Syzygy tables
    are optional downloaded artifacts, so they are reported as informational
    booleans and never change the ready flag.

    Args:
        request: The incoming request, used to read ``request.app.state``.

    Returns:
        A ``(body, status_code)`` pair: the JSON-serializable body and the HTTP
        status code, 200 when ready and 503 otherwise.
    """
    ready = bool(getattr(request.app.state, "ready", True))
    opening_book_loaded = getattr(request.app.state, "opening_book", None) is not None
    tablebase_loaded = getattr(request.app.state, "tablebase", None) is not None

    body = {
        "status": "ready" if ready else "not_ready",
        "opening_book": opening_book_loaded,
        "tablebase": tablebase_loaded,
    }
    logger.debug(
        "readiness_check",
        ready=ready,
        opening_book=opening_book_loaded,
        tablebase=tablebase_loaded,
    )

    status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return body, status_code


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict:
    """Liveness probe.

    Returns 200 whenever the process is alive and the event loop is responsive.
    Reads no application state and touches no engine resource.

    Returns:
        A small status object identifying the service.
    """
    return {"status": "ok", "service": "blitzy-chess"}


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe.

    Reports whether application startup has completed and which optional
    resources are loaded. Returns 200 when ready and 503 otherwise.

    Args:
        request: The incoming request, used to read ``request.app.state``.

    Returns:
        A JSON response with the readiness body and the matching status code.
    """
    body, status_code = _readiness_payload(request)
    return JSONResponse(content=body, status_code=status_code)


@router.get("/ready")
async def ready_alias(request: Request) -> JSONResponse:
    """Readiness probe alias.

    Convenience alias for ``/health/ready`` with identical behavior.

    Args:
        request: The incoming request, used to read ``request.app.state``.

    Returns:
        A JSON response with the readiness body and the matching status code.
    """
    body, status_code = _readiness_payload(request)
    return JSONResponse(content=body, status_code=status_code)
