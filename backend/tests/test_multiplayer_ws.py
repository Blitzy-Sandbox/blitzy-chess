"""End-to-end tests for the ``/ws/multiplayer`` two-player WebSocket endpoint.

These tests exercise :func:`chess_ai.api.multiplayer_ws.multiplayer_endpoint`
through the real FastAPI application using the synchronous ``TestClient`` from
``conftest``. Two concurrent ``websocket_connect("/ws/multiplayer")`` contexts
model the two players, because ``httpx`` cannot open a WebSocket scope.

The suite walks the full room lifecycle -- create, join, move, resign, and
reconnect -- and checks the broadcast semantics: a direct reply goes to one
socket, while a shared ``state`` or ``game_over`` fans out to both. The central
case is the server-authoritative rejection of an illegal move (Constraint 12):
the offending player receives an ``error`` and the opponent receives nothing for
that move.

Two design points keep the suite fast and reliable:

* WebSocket reads are bounded -- through the ``recv_until`` helper, or (for the
  opponent-no-frame case) a background read with an explicit timeout -- so a
  missing or mistyped broadcast fails as a quick assertion instead of blocking
  the test forever.
* The endpoint backs every connection with one module-level ``RoomManager``
  singleton (:data:`chess_ai.api.multiplayer_ws.manager`). An autouse fixture
  clears its room registry before and after each test so the random-code rooms
  (and any disconnect timers) of one test never leak into the next.
"""

import concurrent.futures
import contextlib
import re

import chess
import pytest

from chess_ai.api.multiplayer_ws import manager

# Position after 1. e4 with Black to move. This is the broadcast FEN asserted by
# the legal-move, illegal-rejection, out-of-turn, and reconnect-replay tests.
_AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

# Final position of Fool's mate (1. f3 e5 2. g4 Qh4#): White to move and mated.
_FOOLS_MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"

# Multiplayer room codes are random 6-character uppercase alphanumerics. The
# tests assert this FORMAT, never the exact (random) value.
_ROOM_CODE_PATTERN = r"[A-Z0-9]{6}"


@pytest.fixture(autouse=True)
def _clean_rooms():
    """Clear the shared ``RoomManager`` registry before and after each test.

    Because every connection resolves through one module-level ``manager``
    singleton, leftover rooms (and their disconnect timers) from a prior test
    would otherwise leak into the next. Clearing ``manager._rooms`` on both sides
    of the ``yield`` keeps the two-player cases isolated.
    """
    manager._rooms.clear()
    yield
    manager._rooms.clear()


def _create_room(ws):
    """Send ``create_room`` on ``ws`` and return the ``room_created`` reply."""
    ws.send_json({"type": "create_room"})
    message = ws.receive_json()
    assert message["type"] == "room_created"
    return message


def _join_room(ws, code):
    """Send ``join_room`` for ``code`` on ``ws`` and return the first reply."""
    ws.send_json({"type": "join_room", "code": code})
    return ws.receive_json()


@contextlib.contextmanager
def _two_player_game(client, recv_until):
    """Open two sockets, seat both players, and yield a ready, active game.

    Creates a room on the first socket (White), joins it on the second (Black),
    then drains the activation ``state`` broadcast from both queues so the caller
    starts from an empty, in-turn position.

    Args:
        client: The synchronous FastAPI ``TestClient`` from ``conftest``.
        recv_until: The bounded ``(ws, msg_type) -> dict`` reader from ``conftest``.

    Yields:
        A tuple ``(white, black, code, white_token, black_token)``: the two open
        WebSocket sessions, the room code, and each player's reconnect token.
    """
    with (
        client.websocket_connect("/ws/multiplayer") as white,
        client.websocket_connect("/ws/multiplayer") as black,
    ):
        created = _create_room(white)
        code = created["code"]
        joined = _join_room(black, code)
        assert joined["type"] == "room_joined"
        # Joining activates the game and broadcasts the start state to BOTH
        # players; drain one frame from each queue so tests begin from a clean
        # slate with White to move.
        recv_until(white, "state")
        recv_until(black, "state")
        yield white, black, code, created["player_token"], joined["player_token"]


def test_create_room_returns_code(client):
    """``create_room`` replies with a 6-char code, a token, and White for the creator."""
    with client.websocket_connect("/ws/multiplayer") as white:
        created = _create_room(white)
        # Assert the code FORMAT, never the exact random value.
        assert re.fullmatch(_ROOM_CODE_PATTERN, created["code"]) is not None
        assert created["color"] == "white"
        token = created["player_token"]
        assert isinstance(token, str)
        assert token


def test_join_room_broadcasts_state_to_both(client, recv_until):
    """Joining seats Black and broadcasts the start ``state`` to both players."""
    with (
        client.websocket_connect("/ws/multiplayer") as white,
        client.websocket_connect("/ws/multiplayer") as black,
    ):
        created = _create_room(white)
        joined = _join_room(black, created["code"])
        # The joiner gets a direct ``room_joined`` reply naming its color.
        assert joined["type"] == "room_joined"
        assert joined["color"] == "black"

        # BOTH players then receive the same authoritative start state.
        white_state = recv_until(white, "state")
        black_state = recv_until(black, "state")
        for state in (white_state, black_state):
            assert state["fen"] == chess.STARTING_FEN
            assert state["turn"] == "white"
            assert state["status"] == "active"


def test_join_unknown_code_errors(client):
    """Joining a non-existent code returns ``room_not_found`` to the joiner."""
    with client.websocket_connect("/ws/multiplayer") as ws:
        ws.send_json({"type": "join_room", "code": "ZZZZZZ"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "room_not_found"


def test_third_player_cannot_join_full_room(client, recv_until):
    """A third client joining a full (two-player) room is rejected with ``room_full``."""
    with _two_player_game(client, recv_until) as (_white, _black, code, _, _):
        with client.websocket_connect("/ws/multiplayer") as third:
            third.send_json({"type": "join_room", "code": code})
            error = third.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "room_full"


def test_legal_move_is_broadcast_to_both(client, recv_until):
    """A legal move from the player on turn is applied and broadcast to both players."""
    with _two_player_game(client, recv_until) as (white, black, _, _, _):
        white.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        white_state = recv_until(white, "state")
        black_state = recv_until(black, "state")
        for state in (white_state, black_state):
            assert state["fen"] == _AFTER_E4_FEN
            assert state["turn"] == "black"
            # The move history grew by exactly one (the SAN for 1. e4).
            assert state["move_history"] == ["e4"]


def test_illegal_move_rejected_and_not_relayed(client, recv_until):
    """Constraint 12: an illegal move errors to the mover only and is never relayed.

    The illegal move is sent FIRST, while it is still White's turn, so the
    server's legality check (not the turn check) is what rejects it. A single
    background read on Black's socket proves the opponent receives NOTHING for the
    rejected move: that read stays pending across the rejection, then resolves
    with the authoritative state only after a later legal move. Reusing one future
    (rather than two sequential reads) avoids frame-stealing -- whatever frame
    Black receives FIRST is exactly the frame asserted below.
    """
    with _two_player_game(client, recv_until) as (white, black, _, _, _):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            # One background read; Black's first frame will resolve this future.
            black_next = pool.submit(black.receive_json)

            # e2->e5 is illegal from the start; the offender alone gets an error.
            white.send_json({"type": "move", "from_square": "e2", "to_square": "e5"})
            error = white.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "illegal_move"

            # White's error has arrived, so the server finished handling the
            # illegal move; any (erroneous) opponent broadcast would already be
            # enqueued. Black's read must still be pending -- the opponent got
            # nothing for the rejected move.
            with pytest.raises(concurrent.futures.TimeoutError):
                black_next.result(timeout=0.5)

            # Now play a legal move. Black's FIRST frame (the still-pending read)
            # must be the authoritative state for e4 -- never a leaked error.
            white.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
            black_state = black_next.result(timeout=5.0)
            assert black_state["type"] == "state"
            assert black_state["fen"] == _AFTER_E4_FEN
            assert black_state["move_history"] == ["e4"]
        finally:
            # Non-blocking shutdown: if an assertion failed with the read still
            # pending, closing the socket (on context exit) unblocks the worker,
            # so the test fails fast instead of hanging on pool join.
            pool.shutdown(wait=False)


def test_move_out_of_turn_rejected(client, recv_until):
    """A move from the player NOT on turn is rejected with ``not_your_turn``."""
    with _two_player_game(client, recv_until) as (white, black, _, _, _):
        # Black tries to move first; it is White's turn, so the manager rejects
        # the move (before any legality check) and broadcasts nothing.
        black.send_json({"type": "move", "from_square": "e7", "to_square": "e5"})
        error = black.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "not_your_turn"

        # Confirm the position never advanced: White's legal e4 yields a history
        # of exactly one move, so Black's out-of-turn attempt was never applied.
        white.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
        white_state = recv_until(white, "state")
        assert white_state["move_history"] == ["e4"]


def test_resign_broadcasts_game_over(client, recv_until):
    """Resigning broadcasts ``game_over`` to both players with the opponent as winner."""
    with _two_player_game(client, recv_until) as (white, black, _, _, _):
        white.send_json({"type": "resign"})
        white_over = recv_until(white, "game_over")
        black_over = recv_until(black, "game_over")
        for over in (white_over, black_over):
            assert over["result"] == "resignation"
            # White resigned, so Black (the opponent) is recorded as the winner.
            assert over["winner"] == "black"


def test_fools_mate_ends_game(client, recv_until):
    """Fool's mate played over the two sockets ends in checkmate with Black winning."""
    with _two_player_game(client, recv_until) as (white, black, _, _, _):
        # 1. f3 e5 2. g4 -- read each broadcast state on both sockets so the
        # queues stay balanced for the final, game-ending move.
        for ws, from_square, to_square in (
            (white, "f2", "f3"),
            (black, "e7", "e5"),
            (white, "g2", "g4"),
        ):
            ws.send_json({"type": "move", "from_square": from_square, "to_square": to_square})
            recv_until(white, "state")
            recv_until(black, "state")

        # 2... Qh4# delivers mate. Each player receives the final state followed
        # by the terminal game_over.
        black.send_json({"type": "move", "from_square": "d8", "to_square": "h4"})
        white_state = recv_until(white, "state")
        assert white_state["fen"] == _FOOLS_MATE_FEN
        white_over = recv_until(white, "game_over")
        black_over = recv_until(black, "game_over")
        for over in (white_over, black_over):
            assert over["result"] == "checkmate"
            assert over["winner"] == "black"


def test_reconnect_returns_current_state(client, recv_until):
    """Reconnecting with the player token replays the live FEN and move history."""
    with client.websocket_connect("/ws/multiplayer") as black:
        # Keep Black open while White drops, to model a mid-game reconnect.
        with client.websocket_connect("/ws/multiplayer") as white:
            created = _create_room(white)
            code = created["code"]
            white_token = created["player_token"]
            joined = _join_room(black, code)
            assert joined["type"] == "room_joined"
            recv_until(white, "state")
            recv_until(black, "state")

            white.send_json({"type": "move", "from_square": "e2", "to_square": "e4"})
            recv_until(white, "state")
            recv_until(black, "state")
        # White's socket is now closed (a simulated drop). A fresh socket
        # reconnects with the same player token and must receive a catch-up state
        # replaying the live position.
        with client.websocket_connect("/ws/multiplayer") as reconnected:
            reconnected.send_json({"type": "reconnect", "code": code, "player_token": white_token})
            state = recv_until(reconnected, "state")
            assert state["fen"] == _AFTER_E4_FEN
            assert state["move_history"] == ["e4"]
