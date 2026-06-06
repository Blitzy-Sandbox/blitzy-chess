"""FastAPI composition root for the blitzy-chess backend.

This module wires the whole application together: structured logging and
tracing, the correlation-id and CORS middleware, the health/readiness, AI-game,
and multiplayer routers, the Prometheus ``/metrics`` exposition, a read-only
``/api/config`` bootstrap route, and static serving of the built frontend with a
single-page-application fallback. The lifespan handler loads the optional
Polyglot opening book and Syzygy tablebases, warms the transposition table, and
stores the shared handles on ``app.state`` for the WebSocket handlers.

The module exposes ``app`` at module scope so it runs as ``chess_ai.app:app``
under Uvicorn from the ``backend/`` directory. Game moves travel over the
WebSocket endpoints only; the REST surface is limited to health, readiness,
metrics, and initial load. The CPU-bound engine search is offloaded by the
endpoints, never by this module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from chess_ai.api import game_ws, health, multiplayer_ws
from chess_ai.config import ensure_dirs, settings
from chess_ai.engine.book import load_book
from chess_ai.engine.endgame import open_tablebase
from chess_ai.engine.search import TranspositionTable
from chess_ai.observability.logging_config import configure_logging, get_logger
from chess_ai.observability.metrics import metrics_app
from chess_ai.observability.tracing import (
    instrument_fastapi_app,
    setup_tracing,
    shutdown_tracing,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["app"]

APP_TITLE = "blitzy-chess"
APP_VERSION = "0.1.0"

# First path segments owned by backend routers. The SPA fallback returns 404 for
# these so it never shadows the API, WebSocket, metrics, or health routes.
_RESERVED_PREFIXES: frozenset[str] = frozenset({"api", "ws", "metrics", "health", "ready"})


# ---------------------------------------------------------------------------
# Observability initialization (configured before the app is created)
# ---------------------------------------------------------------------------
configure_logging()
logger = get_logger(__name__)
setup_tracing(service_name=APP_TITLE)


# ---------------------------------------------------------------------------
# Lifespan: load shared resources on startup, release them on shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load shared resources on startup and release them on shutdown.

    Startup ensures the runtime data directories exist, loads the optional
    opening book, warms the transposition table, and opens the optional Syzygy
    tablebases, storing the handles on ``app.state``. Missing optional artifacts
    are logged and skipped, so startup never fails on their absence. The
    readiness flag is set last. Shutdown closes the book and tablebase handles
    and flushes the tracer.

    Args:
        app: The application whose ``state`` receives the shared handles.

    Yields:
        Control to the running application.
    """
    app.state.opening_book = None
    app.state.tablebase = None
    app.state.transposition_table = None
    app.state.ready = False

    ensure_dirs()

    try:
        app.state.opening_book = load_book(settings.OPENING_BOOK_PATH)
    except Exception as exc:
        logger.warning("opening_book_load_failed", error=str(exc))
        app.state.opening_book = None
    logger.info("opening_book_ready", loaded=app.state.opening_book is not None)

    try:
        app.state.transposition_table = TranspositionTable()
        logger.info(
            "transposition_table_warmed",
            buckets=app.state.transposition_table.num_buckets,
        )
    except Exception as exc:
        logger.warning("transposition_table_warm_failed", error=str(exc))
        app.state.transposition_table = None

    try:
        app.state.tablebase = open_tablebase(settings.TABLES_DIR)
    except Exception as exc:
        logger.warning("tablebase_open_failed", error=str(exc))
        app.state.tablebase = None
    logger.info("tablebase_ready", loaded=app.state.tablebase is not None)

    app.state.ready = True
    logger.info("startup_complete")

    try:
        yield
    finally:
        book = getattr(app.state, "opening_book", None)
        if book is not None:
            with suppress(Exception):
                book.close()
        tablebase = getattr(app.state, "tablebase", None)
        if tablebase is not None:
            with suppress(Exception):
                tablebase.close()
        app.state.ready = False
        shutdown_tracing()
        logger.info("shutdown_complete")


# ---------------------------------------------------------------------------
# Application and middleware
# ---------------------------------------------------------------------------
app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

instrument_fastapi_app(app)


# ---------------------------------------------------------------------------
# Routers and endpoints (registered before the static fallback)
# ---------------------------------------------------------------------------
app.include_router(health.router)
app.include_router(game_ws.router)
app.include_router(multiplayer_ws.router)


@app.get("/metrics", include_in_schema=False)
async def metrics_root() -> Response:
    """Redirect the bare ``/metrics`` path to the mounted exposition at ``/metrics/``.

    Returns:
        A 307 redirect to ``/metrics/``.
    """
    return Response(status_code=307, headers={"Location": "/metrics/"})


app.mount("/metrics", metrics_app())


@app.get("/api/config", tags=["bootstrap"])
async def get_config() -> dict:
    """Return read-only bootstrap metadata for the SPA's initial load.

    Exposes the difficulty tiers and the move-pacing constants. This route
    handles no chess moves; game moves travel over the WebSocket endpoints only.

    Returns:
        The difficulty tiers (name, depth, time budgets) and pacing constants.
    """
    return {
        "difficulties": [
            {
                "name": tier.name,
                "depth": tier.depth,
                "time_budget_s": tier.time_budget_s,
                "time_budget_ms": tier.time_budget_ms,
            }
            for tier in settings.DIFFICULTY_TIERS.values()
        ],
        "min_ai_delay_ms": settings.MIN_AI_DELAY_MS,
        "self_play_move_delay_ms": settings.SELF_PLAY_MOVE_DELAY_MS,
    }


# ---------------------------------------------------------------------------
# Static frontend serving with SPA fallback (mounted last)
# ---------------------------------------------------------------------------
def _is_reserved_path(path: str) -> bool:
    """Return ``True`` when ``path``'s first segment is owned by a backend route.

    Args:
        path: The request path with no leading slash (the catch-all capture).

    Returns:
        ``True`` for API, WebSocket, metrics, and health paths.
    """
    return path.split("/", 1)[0] in _RESERVED_PREFIXES


def _mount_frontend(application: FastAPI, dist_dir: Path) -> None:
    """Mount the built frontend with a single-page-application fallback.

    Real files are served from ``dist_dir``; any other non-backend GET path
    returns ``index.html`` so client-side routing works on refresh and deep
    links. Backend routes are registered earlier and take precedence.

    Args:
        application: The application to register the static routes on.
        dist_dir: The built frontend directory (``frontend/dist``).
    """
    dist_root = dist_dir.resolve()
    index_file = dist_root / "index.html"
    assets_dir = dist_root / "assets"

    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        """Serve a built asset, or fall back to ``index.html`` for SPA routes."""
        if _is_reserved_path(full_path):
            return Response(status_code=404)
        candidate = (dist_root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_root):
            return FileResponse(candidate)
        if index_file.is_file():
            return FileResponse(index_file)
        return Response(status_code=404)


if settings.FRONTEND_DIST_DIR.is_dir():
    _mount_frontend(app, settings.FRONTEND_DIST_DIR)
    logger.info("frontend_static_mounted", dist=str(settings.FRONTEND_DIST_DIR))
else:
    logger.info("frontend_dist_absent", dist=str(settings.FRONTEND_DIST_DIR))
