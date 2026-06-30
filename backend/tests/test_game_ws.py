"""End-to-end tests for the ``/ws/game`` single-player AI WebSocket endpoint.

These tests drive :func:`chess_ai.api.game_ws.game_endpoint` through the real
FastAPI application using the synchronous ``TestClient`` from ``conftest``.
``httpx`` cannot open a WebSocket scope, so every case here is a PLAIN SYNC
function (never ``async``) that uses ``client.websocket_connect(...)`` together
with ``send_json`` / ``receive_json``.

The real engine search can take seconds, so it is replaced with an instant
:class:`FakeSearcher` that returns the first legal move for the *actual* board.
The suite covers the connection handshake, the human-move -> AI-reply flow,
server-authoritative illegal-move rejection (Constraint 12), AI-first play when
the human is Black, resignation, streamed ``ai_thinking`` progress, and the
minimum AI move pacing (Constraint 8).

Pacing note
-----------
Every test except :func:`test_ai_reply_is_paced` patches ``MIN_AI_DELAY_MS`` to
zero through the ``fast_ai`` fixture so the suite stays fast. The pacing test
keeps the real 1500 ms delay and asserts the AI reply is held back accordingly,
which proves even an instant (book/tablebase-style) move still honors the floor.

Verified chess facts (do not re-derive):
* From the start position ``e2->e4`` is LEGAL and ``e2->e5`` is ILLEGAL.
* ``chess.STARTING_FEN`` is the canonical initial-position FEN.
"""

import time
from types import SimpleNamespace

import chess
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from chess_ai.api import health as health_module
from chess_ai.api.game_ws import _CLOSED_SOCKET_MESSAGE, _send_or_disconnect


class FakeSearcher:
    """Instant, deterministic stand-in for the engine ``Searcher``.

    ``search`` returns at once with the first legal move for the board it is
    handed, so the reply is always legal, and it duck-types the engine's
    ``SearchResult`` with a ``SimpleNamespace`` instead of importing the engine.
    Setting the class attribute ``emit_thinking`` to ``True`` (see
    :class:`ThinkingSearcher`) also streams one ``ai_thinking`` update through
    the ``info_callback`` the endpoint passes into ``search``.
    """

    # When True, ``search`` invokes ``info_callback`` once to emit progress.
    emit_thinking = False

    def __init__(self, *args, **kwargs) -> None:
        # Tolerate whatever the endpoint constructs the searcher with, e.g.
        # ``Searcher(book=..., tablebase=...)``; nothing needs to be stored.
        pass

    def search(self, board, limits=None, info_callback=None, *args, **kwargs):
        """Return the first legal move for ``board`` as a duck-typed result."""
        move = next(iter(board.legal_moves))
        if self.emit_thinking and info_callback is not None:
            # The endpoint renders SAN from ``info.pv``, so the pv must hold real
            # moves; the remaining fields duck-type the engine ``SearchInfo``.
            info_callback(SimpleNamespace(depth=1, score_cp=0, pv=[move], nodes=1, time_s=0.0))
        return SimpleNamespace(
            best_move=move,
            score_cp=0,
            depth=1,
            pv=[move],
            nodes=1,
            time_s=0.0,
            ranked_moves=[(move, 0)],
            from_book=False,
            from_tablebase=False,
        )


class ThinkingSearcher(FakeSearcher):
    """:class:`FakeSearcher` variant that streams one ``ai_thinking`` update."""

    emit_thinking = True


@pytest.fixture
def fast_ai(monkeypatch):
    """Install the instant :class:`FakeSearcher` and zero the AI move delay.

    Used by every test except :func:`test_ai_reply_is_paced`. The endpoint builds
    its ``Searcher`` from the ``chess_ai.api.game_ws`` namespace and reads
    ``MIN_AI_DELAY_MS`` from that same namespace (it did ``from chess_ai.config
    import MIN_AI_DELAY_MS``), so both names are patched there; the source
    constant in ``chess_ai.config`` is patched too for completeness.
    """
    monkeypatch.setattr("chess_ai.api.game_ws.Searcher", FakeSearcher, raising=False)
    monkeypatch.setattr("chess_ai.api.game_ws.MIN_AI_DELAY_MS", 0, raising=False)
    monkeypatch.setattr("chess_ai.config.MIN_AI_DELAY_MS", 0, raising=False)


def _read_ai_reply(ws, recv_until, max_rounds=4):
    """Return the ``state`` frame in which the AI has replied to a human move.

    After a legal human (White) move the endpoint sends one ``state`` for the
    human move (Black to move) and then, once the threaded search finishes,
    another ``state`` for the AI's reply (White to move again). This skips the
    interim human-move state and returns the AI-reply state. The bounded loop
    surfaces a missing reply as a fast assertion instead of blocking forever.

    Args:
        ws: The open ``TestClient`` WebSocket session.
        recv_until: The bounded ``(ws, msg_type) -> dict`` reader from ``conftest``.
        max_rounds: How many ``state`` frames to read before giving up.

    Returns:
        The AI-reply ``state`` message (``turn == "white"`` or >= 2 half-moves).
    """
    for _ in range(max_rounds):
        state = recv_until(ws, "state")
        if state["turn"] == "white" or len(state["move_history"]) >= 2:
            return state
    raise AssertionError("the AI did not send a reply state within the expected frames")


def test_connect_sends_initial_state(client, fast_ai):
    """On connect the server sends the start position as the first ``state``."""
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        state = ws.receive_json()
    assert state["type"] == "state"
    # The FEN both equals the canonical start FEN and parses to the start board.
    assert state["fen"] == chess.STARTING_FEN
    assert chess.Board(state["fen"]) == chess.Board()
    assert state["turn"] == "white"
    assert state["move_history"] == []


def test_human_move_gets_ai_reply(client, recv_until, fast_ai):
    """A legal human move is applied and the AI replies with an advanced state."""
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        ws.receive_json()  # initial state
        ws.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        state = _read_ai_reply(ws, recv_until)
    # White's e4 leads the history and the AI has appended its own reply.
    assert state["move_history"][0] == "e4"
    assert len(state["move_history"]) >= 2
    assert state["turn"] == "white"  # back to the human's turn after the AI moved
    assert state["fen"] != chess.STARTING_FEN
    chess.Board(state["fen"])  # the advanced FEN is well-formed


def test_illegal_move_is_rejected(client, recv_until, fast_ai):
    """Constraint 12: an illegal move is rejected and never advances the game.

    ``e2->e5`` is illegal from the start position, so the server replies with an
    ``illegal_move`` error and leaves the position untouched. A follow-up legal
    ``e2->e4`` is then accepted as the FIRST move, proving the rejected attempt
    neither corrupted the board nor dropped the connection.
    """
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        ws.receive_json()  # initial state
        ws.send_json({"type": "move", "from_square": "e2", "to_square": "e5"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "illegal_move"
        # The position must not have advanced: a legal e2->e4 is now accepted and
        # becomes the FIRST move of the game.
        ws.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        state = _read_ai_reply(ws, recv_until)
    assert state["move_history"][0] == "e4"
    assert len(state["move_history"]) >= 2


def test_ai_moves_first_when_human_is_black(client, recv_until, fast_ai):
    """When the human is Black, the AI (White) makes the opening move."""
    with client.websocket_connect("/ws/game?difficulty=easy&color=black") as ws:
        initial = ws.receive_json()
        assert initial["type"] == "state"
        assert initial["move_history"] == []
        assert initial["turn"] == "white"
        # The human is Black, so the AI (White) opens; the next state shows it.
        ai_state = recv_until(ws, "state")
    assert len(ai_state["move_history"]) >= 1
    assert ai_state["turn"] == "black"


def test_resign_ends_game(client, recv_until, fast_ai):
    """Resigning ends the game with the AI (the opponent) recorded as winner."""
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        ws.receive_json()  # initial state
        ws.send_json({"type": "resign"})
        game_over = recv_until(ws, "game_over")
    assert game_over["result"] == "resignation"
    # The human is White, so the AI (Black) is the winner.
    assert game_over["winner"] == "black"


def test_ai_thinking_updates_emitted(client, fast_ai, monkeypatch):
    """At least one ``ai_thinking`` update precedes the AI's reply state.

    The default ``FakeSearcher`` is swapped for :class:`ThinkingSearcher`, which
    invokes the endpoint's ``info_callback`` once; the endpoint marshals that into
    an ``ai_thinking`` frame streamed before the terminal AI ``state``.
    """
    monkeypatch.setattr("chess_ai.api.game_ws.Searcher", ThinkingSearcher, raising=False)
    saw_thinking = False
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        ws.receive_json()  # initial state
        ws.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        # Read frames up to the AI's reply state, noting any ai_thinking en route.
        for _ in range(12):
            msg = ws.receive_json()
            if msg["type"] == "ai_thinking":
                saw_thinking = True
                assert isinstance(msg["depth"], int)
                assert isinstance(msg["evaluation"], int)
                assert isinstance(msg["pv"], list)
            if msg["type"] == "state" and (msg["turn"] == "white" or len(msg["move_history"]) >= 2):
                break
    assert saw_thinking, "expected at least one ai_thinking before the AI state"


def test_ai_reply_is_paced(client, recv_until, monkeypatch):
    """Constraint 8: the AI reply is held back to at least ``MIN_AI_DELAY_MS``.

    Only the (instant) search is faked here; the real 1500 ms delay is kept, so
    the elapsed time measured is pure pacing rather than compute. A small slack
    below 1.5 s absorbs scheduling jitter.
    """
    monkeypatch.setattr("chess_ai.api.game_ws.Searcher", FakeSearcher, raising=False)
    with client.websocket_connect("/ws/game?difficulty=easy&color=white") as ws:
        ws.receive_json()  # initial state
        start = time.monotonic()
        ws.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        _read_ai_reply(ws, recv_until)
        elapsed = time.monotonic() - start
    assert elapsed >= 1.4


def test_health_ok(client):
    """The REST health probe answers 200 via the same synchronous client."""
    response = client.get("/health")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Disconnect-during-search hygiene (regression for QA finding F-1)
# ---------------------------------------------------------------------------
class _RecordingWebSocket:
    """Minimal ``WebSocket`` stand-in for unit-testing ``_send_or_disconnect``.

    Mimics only what the helper touches: the ``application_state`` attribute and
    an awaitable ``send_text``. ``send_text`` records each payload, or raises a
    preconfigured exception to simulate a socket that has been closed (abruptly
    or gracefully) while the AI was searching off the event loop.
    """

    def __init__(self, *, application_state, send_exc=None):
        self.application_state = application_state
        self._send_exc = send_exc
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        if self._send_exc is not None:
            raise self._send_exc
        self.sent.append(data)


async def test_send_or_disconnect_skips_send_when_already_disconnected():
    """F-1: a socket already flipped to DISCONNECTED unwinds as a disconnect.

    When the ai_thinking drain (or an earlier send) has already observed the
    client's disconnect, Starlette has set ``application_state`` to
    ``DISCONNECTED``. The helper must NOT attempt the doomed post-search send
    (which would raise the bare closed-socket ``RuntimeError``); it raises a
    benign ``WebSocketDisconnect`` instead, which the endpoint logs at INFO.
    """
    ws = _RecordingWebSocket(application_state=WebSocketState.DISCONNECTED)
    with pytest.raises(WebSocketDisconnect):
        await _send_or_disconnect(ws, '{"type": "state"}')
    # The doomed send must never be attempted on a disconnected socket.
    assert ws.sent == []


async def test_send_or_disconnect_reclassifies_closed_socket_runtimeerror():
    """F-1: the closed-socket ``RuntimeError`` is reclassified as a disconnect.

    If an abrupt drop races the state guard so the send itself raises Starlette's
    closed-socket ``RuntimeError``, the helper reclassifies it as a benign
    ``WebSocketDisconnect`` rather than letting it surface as an ERROR-level
    traceback through the endpoint's broad ``except Exception``.
    """
    closed = RuntimeError(_CLOSED_SOCKET_MESSAGE)
    ws = _RecordingWebSocket(application_state=WebSocketState.CONNECTED, send_exc=closed)
    with pytest.raises(WebSocketDisconnect):
        await _send_or_disconnect(ws, '{"type": "state"}')


async def test_send_or_disconnect_propagates_genuine_runtimeerror():
    """F-1: a genuine ``RuntimeError`` is NOT masked as a disconnect.

    Only the closed-socket runtime error is benign. Any other ``RuntimeError`` is
    an unexpected failure and must propagate unchanged so the endpoint can still
    log it as an error (the broad ``except Exception`` stays meaningful).
    """
    genuine = RuntimeError("genuine unexpected failure")
    ws = _RecordingWebSocket(application_state=WebSocketState.CONNECTED, send_exc=genuine)
    with pytest.raises(RuntimeError) as excinfo:
        await _send_or_disconnect(ws, '{"type": "state"}')
    # It must remain the genuine error, never reclassified to a disconnect.
    assert not isinstance(excinfo.value, WebSocketDisconnect)
    assert "genuine unexpected failure" in str(excinfo.value)


async def test_send_or_disconnect_sends_when_connected():
    """F-1: the happy path is unchanged -- a connected socket sends normally."""
    ws = _RecordingWebSocket(application_state=WebSocketState.CONNECTED)
    payload = '{"type": "state"}'
    await _send_or_disconnect(ws, payload)
    assert ws.sent == [payload]


# ---------------------------------------------------------------------------
# Readiness probe (Info-2): direct coverage of health._readiness_payload and the
# /health/ready + /ready route handlers, independent of the application lifespan.
# ---------------------------------------------------------------------------
# These tests deliberately do NOT use the session-scoped ``client`` fixture: that
# client enters the real application lifespan, which sets ``app.state.ready`` on
# the shared global app, so a readiness assertion against it would depend on test
# ordering. Each test below builds an ISOLATED FastAPI app that mounts only the
# health router and whose ``app.state`` is set explicitly here -- making the
# ready / not-ready / startup-missing branches deterministic while also exercising
# the route handlers and their 200/503 status mapping.
def _readiness_client(**state) -> TestClient:
    """Build a ``TestClient`` over a minimal app exposing only the health router.

    The app carries no lifespan and no engine resources, so the readiness probe is
    driven solely by the ``app.state`` attributes passed here.

    Args:
        **state: Attributes to set on ``app.state`` (e.g. ``ready=True``,
            ``opening_book=object()``). Omit ``ready`` entirely to simulate an
            application whose startup never initialized the flag.

    Returns:
        A ``TestClient`` bound to the isolated health-only app.
    """
    isolated = FastAPI()
    isolated.include_router(health_module.router)
    for name, value in state.items():
        setattr(isolated.state, name, value)
    return TestClient(isolated)


def test_readiness_fails_closed_when_startup_flag_missing():
    """No ``app.state.ready`` -> the probe fails closed: 503 with ``startup_state`` missing."""
    with _readiness_client() as ready_client:
        response = ready_client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # A never-initialized flag is surfaced explicitly so operators can tell
    # "startup has not run" apart from "startup ran but is not ready yet".
    assert body["startup_state"] == "missing"
    assert body["opening_book"] is False
    assert body["tablebase"] is False


def test_readiness_reports_ready_when_startup_complete():
    """Truthy ``ready`` flag -> 200 ready; loaded optional resources surface as ``True``."""
    with _readiness_client(ready=True, opening_book=object(), tablebase=object()) as ready_client:
        response = ready_client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["opening_book"] is True
    assert body["tablebase"] is True
    # ``startup_state`` is emitted ONLY when the flag was never initialized.
    assert "startup_state" not in body


def test_readiness_not_ready_when_flag_explicitly_false():
    """An explicit ``ready=False`` -> 503 not_ready, and NO ``startup_state`` (flag present)."""
    with _readiness_client(ready=False) as ready_client:
        response = ready_client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert "startup_state" not in body
    assert body["opening_book"] is False
    assert body["tablebase"] is False


def test_ready_alias_matches_health_ready():
    """The ``/ready`` alias returns the same body and status as ``/health/ready``."""
    with _readiness_client(ready=True) as ready_client:
        primary = ready_client.get("/health/ready")
        alias = ready_client.get("/ready")
    assert alias.status_code == primary.status_code == 200
    assert alias.json() == primary.json()
