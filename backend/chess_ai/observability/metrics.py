"""Prometheus metrics for the blitzy-chess backend.

Defines every application metric once on the default Prometheus registry and
builds the ASGI app that serves them in Prometheus text format. ``chess_ai.app``
mounts that app at ``/metrics`` via :func:`metrics_app` or :func:`setup_metrics`;
the ``chess_ai.api`` layer records events through the thin helpers below so it
never touches the metric objects directly.

Metric names follow the ``chess_ai_`` prefix and Prometheus conventions
(``_total`` on counters, base-unit names on histograms). Labels are restricted
to small, fixed enumerations so cardinality stays bounded. This module mounts
nothing and configures nothing at import time, and the pure ``chess_ai.engine``
package never imports it.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


__all__ = [
    "metrics_app",
    "setup_metrics",
    "track_active_game",
    "track_ws_connection",
    "record_search",
    "inc_move",
    "inc_illegal_move",
    "record_game_result",
    "inc_book_move",
    "inc_tablebase_move",
    "time_search",
    "ACTIVE_GAMES",
    "WEBSOCKET_CONNECTIONS",
    "MOVES_PROCESSED",
    "ILLEGAL_MOVES_REJECTED",
    "SEARCH_DURATION",
    "NODES_SEARCHED",
    "GAMES_TOTAL",
    "BOOK_MOVES",
    "TABLEBASE_MOVES",
]


# ---------------------------------------------------------------------------
# Metric definitions (default registry, defined once at module scope)
# ---------------------------------------------------------------------------
# Number of in-progress games, by mode ("ai" or "multiplayer").
ACTIVE_GAMES = Gauge(
    "chess_ai_active_games",
    "Number of in-progress games.",
    ["mode"],
)

# Number of open WebSocket connections, by endpoint ("game" or "multiplayer").
WEBSOCKET_CONNECTIONS = Gauge(
    "chess_ai_websocket_connections",
    "Number of open WebSocket connections.",
    ["endpoint"],
)

# Total moves the server validated, applied, or relayed, by mode.
MOVES_PROCESSED = Counter(
    "chess_ai_moves_processed_total",
    "Total chess moves processed by the server.",
    ["mode"],
)

# Total inbound moves rejected by server-side board.is_legal() validation.
ILLEGAL_MOVES_REJECTED = Counter(
    "chess_ai_illegal_moves_rejected_total",
    "Total illegal moves rejected by server-side validation.",
    ["endpoint"],
)

# AI search wall-clock duration per move in seconds, by difficulty. Buckets
# span the per-tier time budgets (easy 3s, medium 8s, hard 15s) with headroom.
SEARCH_DURATION = Histogram(
    "chess_ai_search_duration_seconds",
    "AI search wall-clock duration per move in seconds.",
    ["difficulty"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 15, 20, 30),
)

# Total search nodes explored by the AI, by difficulty.
NODES_SEARCHED = Counter(
    "chess_ai_nodes_searched_total",
    "Total search nodes explored by the AI.",
    ["difficulty"],
)

# Games completed by terminal result, by mode and result.
GAMES_TOTAL = Counter(
    "chess_ai_games_total",
    "Games completed by terminal result.",
    ["mode", "result"],
)

# Total moves served from the opening book.
BOOK_MOVES = Counter(
    "chess_ai_book_moves_total",
    "Moves served from the opening book.",
)

# Total moves served from Syzygy endgame tablebases.
TABLEBASE_MOVES = Counter(
    "chess_ai_tablebase_moves_total",
    "Moves served from Syzygy tablebases.",
)


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------
def metrics_app():
    """Return a bare ASGI app that serves the default registry's exposition.

    The caller mounts this at ``/metrics``; for example ``app.py`` runs
    ``app.mount("/metrics", metrics_app())``. The app answers GET with the
    Prometheus text-format exposition of every metric defined here.

    Returns:
        An ASGI application serving the Prometheus exposition.
    """
    return make_asgi_app()


def setup_metrics(app: FastAPI, path: str = "/metrics") -> None:
    """Mount the metrics exposition app onto ``app`` at ``path``.

    A convenience so ``app.py`` can delegate the mount while route registration
    still originates from the composition root. The body uses only
    ``app.mount(...)``, so the module stays importable without FastAPI present.

    Args:
        app: The application to mount onto; only ``app.mount`` is used.
        path: Mount path for the exposition; defaults to ``"/metrics"``.
    """
    app.mount(path, make_asgi_app())


# ---------------------------------------------------------------------------
# Instrument helpers for the API layer
# ---------------------------------------------------------------------------
@contextmanager
def track_active_game(mode: str = "ai") -> Iterator[None]:
    """Count one active game for the duration of the ``with`` block.

    Increments :data:`ACTIVE_GAMES` on entry and decrements it in a ``finally``,
    so the gauge returns to its prior value even when the body raises.

    Args:
        mode: Game mode label, ``"ai"`` or ``"multiplayer"``.

    Yields:
        ``None``.
    """
    ACTIVE_GAMES.labels(mode).inc()
    try:
        yield
    finally:
        ACTIVE_GAMES.labels(mode).dec()


@contextmanager
def track_ws_connection(endpoint: str) -> Iterator[None]:
    """Count one open WebSocket connection for the duration of the block.

    Increments :data:`WEBSOCKET_CONNECTIONS` on entry and decrements it in a
    ``finally``, so the gauge is balanced even when the body raises.

    Args:
        endpoint: Endpoint label, ``"game"`` or ``"multiplayer"``.

    Yields:
        ``None``.
    """
    WEBSOCKET_CONNECTIONS.labels(endpoint).inc()
    try:
        yield
    finally:
        WEBSOCKET_CONNECTIONS.labels(endpoint).dec()


def record_search(duration_s: float, nodes: int, difficulty: str) -> None:
    """Record one completed AI search.

    Observes the search duration on :data:`SEARCH_DURATION` and adds the node
    count to :data:`NODES_SEARCHED`, both under the difficulty label. Callers
    pass ``SearchResult.time_s`` and ``SearchResult.nodes``.

    Args:
        duration_s: Search wall-clock duration in seconds.
        nodes: Number of nodes explored in the search.
        difficulty: Difficulty label, ``"easy"``, ``"medium"``, or ``"hard"``.
    """
    SEARCH_DURATION.labels(difficulty).observe(duration_s)
    NODES_SEARCHED.labels(difficulty).inc(nodes)


def inc_move(mode: str = "ai") -> None:
    """Count one move processed by the server.

    Args:
        mode: Game mode label, ``"ai"`` or ``"multiplayer"``.
    """
    MOVES_PROCESSED.labels(mode).inc()


def inc_illegal_move(endpoint: str) -> None:
    """Count one inbound move rejected by server-side validation.

    Args:
        endpoint: Endpoint label, ``"game"`` or ``"multiplayer"``.
    """
    ILLEGAL_MOVES_REJECTED.labels(endpoint).inc()


def record_game_result(result: str, mode: str) -> None:
    """Count one completed game by its terminal result.

    Args:
        result: Terminal result, one of ``"checkmate"``, ``"stalemate"``,
            ``"draw"``, ``"resignation"``, or ``"timeout"``.
        mode: Game mode label, ``"ai"`` or ``"multiplayer"``.
    """
    GAMES_TOTAL.labels(mode=mode, result=result).inc()


def inc_book_move() -> None:
    """Count one move served from the opening book."""
    BOOK_MOVES.inc()


def inc_tablebase_move() -> None:
    """Count one move served from the Syzygy tablebases."""
    TABLEBASE_MOVES.inc()


@contextmanager
def time_search(difficulty: str) -> Iterator[None]:
    """Time a search block and observe its duration on :data:`SEARCH_DURATION`.

    An alternative to :func:`record_search` for callers that prefer to time a
    block rather than pass a precomputed duration. The duration is observed in a
    ``finally``, so it is recorded even when the body raises. This does not touch
    :data:`NODES_SEARCHED`; use :func:`record_search` to record node counts.

    Args:
        difficulty: Difficulty label, ``"easy"``, ``"medium"``, or ``"hard"``.

    Yields:
        ``None``.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        SEARCH_DURATION.labels(difficulty).observe(time.perf_counter() - start)
