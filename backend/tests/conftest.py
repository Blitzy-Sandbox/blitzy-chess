"""Shared pytest fixtures and helpers for the ``chess_ai`` backend test suite.

Every other suite in ``backend/tests/`` builds on these primitives:

* ``START_FEN`` -- the canonical starting-position FEN (``chess.STARTING_FEN``),
  re-exported so suites can assert against a stable reference without importing
  ``chess`` themselves.
* ``make_board`` -- a factory fixture that builds ``chess.Board`` instances from an
  optional FEN.
* ``client`` -- a session-scoped synchronous ``fastapi.testclient.TestClient`` entered as a
  context manager so the application lifespan is active. Use it for BOTH the WebSocket
  endpoints (``/ws/game``, ``/ws/multiplayer``) and the REST surface.
* ``async_client`` -- a function-scoped ``httpx.AsyncClient`` bound to the app through
  ``httpx.ASGITransport``, for REST/health checks ONLY. ASGITransport cannot open a
  WebSocket scope, so it must never be pointed at ``/ws/...``.
* ``recv_until`` -- a helper (exposed both as a plain callable and as a fixture) that reads
  JSON frames off a WebSocket until one of the wanted ``type`` arrives, skipping the
  interleaved ``ai_thinking`` updates the AI endpoint streams before its terminal frame.

Tests run from ``backend/`` (pytest's rootdir, where ``pyproject.toml`` sets
``testpaths = ["tests"]`` and ``asyncio_mode = "auto"``). That directory is auto-prepended
to ``sys.path``, so ``import chess_ai...`` resolves with no path manipulation here. The
rationale for the session-scoped client and the two-transport split is recorded in
docs/decision-log.md.
"""

import chess
import httpx
import pytest
from fastapi.testclient import TestClient

from chess_ai.app import app

# The canonical starting-position FEN, re-exported so suites can assert against a stable
# reference (for example, the initial ``state`` message FEN) without importing ``chess``.
START_FEN = chess.STARTING_FEN


@pytest.fixture
def make_board():
    """Return a factory that builds a ``chess.Board`` from an optional FEN.

    The returned callable yields a fresh board on every call, so a single test can spin up
    several independent positions::

        def test_example(make_board):
            start = make_board()                              # standard initial position
            endgame = make_board("8/8/8/8/8/8/8/K6k w - - 0 1")  # bare-kings endgame

    Returns:
        A callable ``_make(fen=None) -> chess.Board``. When ``fen`` is ``None`` or empty it
        returns the standard starting position; otherwise it parses ``fen``.
    """

    def _make(fen: str | None = None) -> chess.Board:
        return chess.Board(fen) if fen else chess.Board()

    return _make


@pytest.fixture(scope="session")
def client():
    """Yield a synchronous FastAPI ``TestClient`` with the application lifespan active.

    Use this client for BOTH transports:

    * WebSocket tests -- ``with client.websocket_connect("/ws/game") as ws: ...`` (the sync
      client is required; ``httpx`` cannot open a WebSocket scope).
    * REST smoke tests -- ``client.get("/health")`` and friends.

    The fixture is session-scoped; the rationale (running the app lifespan exactly once) is
    recorded in docs/decision-log.md.

    Yields:
        A live ``fastapi.testclient.TestClient`` bound to the application.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client():
    """Yield an ``httpx.AsyncClient`` bound to the app for REST/health checks ONLY.

    The client talks to the FastAPI app in-process through ``httpx.ASGITransport``, so no
    network socket is opened. ``asyncio_mode = "auto"`` (set in ``pyproject.toml``) lets this
    ``async`` fixture run without an explicit marker.

    Note:
        ASGITransport does NOT implement the WebSocket scope -- never use this client for
        ``/ws/...`` endpoints. Drive WebSocket tests with the synchronous ``client`` fixture.

    Yields:
        A live ``httpx.AsyncClient`` whose requests are dispatched to the application.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _recv_until(ws, msg_type, max_msgs: int = 12):
    """Receive JSON frames from ``ws`` until one's ``type`` equals ``msg_type``; return it.

    The AI game endpoint streams ``ai_thinking`` progress updates before it emits the terminal
    ``state``/``game_over`` frame. This helper reads past those interleaved updates to the first
    frame of the wanted type. The loop is bounded so a missing or mistyped message surfaces as a
    fast, clear assertion failure instead of blocking the test indefinitely.

    Args:
        ws: An open WebSocket connection exposing ``receive_json()`` (a ``TestClient`` socket).
        msg_type: The value of the ``"type"`` field to wait for (e.g. ``"state"``).
        max_msgs: The maximum number of frames to read before giving up. Defaults to ``12``.

    Returns:
        The first received message (a ``dict``) whose ``"type"`` equals ``msg_type``.

    Raises:
        AssertionError: If no matching frame arrives within ``max_msgs`` reads.
    """
    last = None
    for _ in range(max_msgs):
        msg = ws.receive_json()
        last = msg
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(
        f"did not receive a {msg_type!r} message within {max_msgs} messages; last was {last!r}"
    )


@pytest.fixture
def recv_until():
    """Return the ``_recv_until`` helper so suites can inject it as a fixture argument.

    Returns:
        The ``_recv_until`` callable ``(ws, msg_type, max_msgs=12) -> dict``.
    """
    return _recv_until
