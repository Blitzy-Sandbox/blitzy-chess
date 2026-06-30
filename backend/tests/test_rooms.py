"""Lifecycle tests for the multiplayer room manager (``chess_ai.rooms.manager``).

The :class:`~chess_ai.rooms.manager.RoomManager` owns every piece of multiplayer
state for two-human games: the 6-character room codes, the two player slots and
their colors, the authoritative ``chess.Board`` per room, the SAN move history,
the disconnect-to-forfeit timer, reconnect replay, and resignation/forfeit
resolution. The WebSocket endpoint (``chess_ai.api.multiplayer_ws``) is only a
transport adapter over this manager, so the manager is exercised here directly,
WITHOUT any sockets or a running FastAPI application.

Coverage spans the full lifecycle:

* Room creation and codes -- format, uniqueness, and registry lookup.
* Joining and slots -- color assignment and rejection of a third joiner or an
  unknown code.
* Server-authoritative moves (Constraint 12) -- a legal move is applied while an
  illegal move is rejected with NO change to the room state, plus out-of-turn
  rejection and a Fool's-mate checkmate that ends the game with Black the winner.
* State snapshots -- ``build_state`` exposes the live FEN, side to move, and the
  move history.
* Reconnect -- a disconnected player gets the full current state back for replay.
* Disconnect timeout -- a player who never returns forfeits to the opponent, and
  reconnecting before the deadline cancels that forfeit.
* Resignation and forfeit -- both end the game with the opponent as the winner.

Determinism and speed: the disconnect timeout is realized with ``asyncio``, so the
timer-driven cases are ``async def`` (the suite runs under ``pytest-asyncio`` with
``asyncio_mode = "auto"``) and inject a tiny timeout, finishing in well under a
second. The verified chess data (the e2e4/e2e5 legality and the Fool's-mate line)
is fixed and is not re-derived here.
"""

import asyncio
import re

import chess
import pytest

from chess_ai.rooms.manager import RoomManager

# Fool's mate (1. f3 e5 2. g4 Qh4#): the shortest forced checkmate. After the
# final move Black has mated White, so the manager reports winner "black".
_FOOLS_MATE_SANS = ["f3", "e5", "g4", "Qh4"]
_FOOLS_MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"

# Room codes are random 6-character uppercase alphanumerics; tests assert this
# FORMAT (length and character class), never the exact (random) value.
_ROOM_CODE_RE = re.compile(r"[A-Z0-9]{6}")

# A tiny disconnect timeout keeps the timer-driven forfeit tests fast and
# non-flaky; those tests wait roughly three times this value before asserting.
_FAST_TIMEOUT_S = 0.05


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------
def make_manager(timeout_s: float = 60.0) -> RoomManager:
    """Return a fresh ``RoomManager`` with an injectable disconnect timeout.

    Args:
        timeout_s: Seconds a disconnected player may stay before forfeiting.
            Timer-driven tests pass a tiny value; everything else uses the
            lenient default so no timer fires mid-test.

    Returns:
        A new, empty ``RoomManager`` instance.
    """
    return RoomManager(disconnect_timeout_s=timeout_s)


@pytest.fixture
def manager() -> RoomManager:
    """Provide a fresh, isolated ``RoomManager`` per test (no shared state)."""
    return make_manager()


def create_two_player_room(mgr: RoomManager) -> tuple[str, str, str]:
    """Create a room and seat two players, returning the handles tests need.

    Args:
        mgr: The manager to create the room in.

    Returns:
        A ``(code, white_token, black_token)`` tuple describing a ready, active
        room: the shareable code and each player's reconnect/move token.
    """
    created = mgr.create_room()
    joined = mgr.join_room(created.code)
    assert joined.type == "room_joined"
    return created.code, created.player_token, joined.player_token


def _play_sans(
    mgr: RoomManager,
    code: str,
    white_token: str,
    black_token: str,
    board: chess.Board,
    sans: list[str],
):
    """Apply a SAN sequence through the manager and return the last result.

    Each SAN is parsed against ``board`` (a mirror of the authoritative
    position) to recover the from/to squares ``apply_move`` expects, choosing
    the moving player's token by whose turn it is. ``board`` is advanced in step
    so callers can compare it against the manager's state afterward.

    Args:
        mgr: The room manager under test.
        code: The room code.
        white_token: White's move token.
        black_token: Black's move token.
        board: A mirror board, mutated in place as moves are applied.
        sans: SAN strings to play in order.

    Returns:
        The ``MoveResult`` from the final applied move.
    """
    result = None
    for san in sans:
        token = white_token if board.turn == chess.WHITE else black_token
        move = board.parse_san(san)
        promotion = chess.piece_symbol(move.promotion) if move.promotion else None
        result = mgr.apply_move(
            code,
            token,
            chess.square_name(move.from_square),
            chess.square_name(move.to_square),
            promotion,
        )
        board.push(move)
    return result


# ---------------------------------------------------------------------------
# Room creation and codes
# ---------------------------------------------------------------------------
def test_create_room_returns_six_char_code(manager):
    """A created room exposes a 6-character uppercase-alphanumeric code."""
    created = manager.create_room()
    assert isinstance(created.code, str)
    assert len(created.code) == 6
    assert _ROOM_CODE_RE.fullmatch(created.code) is not None
    # The creator is seated as White and handed a reconnect token.
    assert created.color == "white"
    assert isinstance(created.player_token, str)
    assert created.player_token


def test_room_codes_are_unique(manager):
    """Distinct rooms receive distinct codes."""
    codes = {manager.create_room().code for _ in range(50)}
    assert len(codes) == 50


def test_created_room_is_retrievable(manager):
    """A created room is found by its code; an unknown code yields ``None``."""
    created = manager.create_room()
    room = manager.get_room(created.code)
    assert room is not None
    assert room.code == created.code
    assert manager.get_room("ZZZZZZ") is None


# ---------------------------------------------------------------------------
# Joining and slots
# ---------------------------------------------------------------------------
def test_second_player_joins_and_gets_other_color(manager):
    """The creator is White, the joiner is Black, and the room becomes full."""
    created = manager.create_room()
    joined = manager.join_room(created.code)
    assert joined.type == "room_joined"
    assert created.color == "white"
    assert joined.color == "black"
    assert created.color != joined.color
    room = manager.get_room(created.code)
    assert room.is_full()
    assert room.status == "active"


def test_third_join_is_rejected(manager):
    """A third join on a full room is rejected with a ``room_full`` error."""
    code, _white_token, _black_token = create_two_player_room(manager)
    third = manager.join_room(code)
    assert third.type == "error"
    assert third.code == "room_full"


def test_join_unknown_code_is_rejected(manager):
    """Joining a non-existent code is rejected with a ``room_not_found`` error."""
    result = manager.join_room("ZZZZZZ")
    assert result.type == "error"
    assert result.code == "room_not_found"


# ---------------------------------------------------------------------------
# Server-authoritative moves (Constraint 12)
# ---------------------------------------------------------------------------
def test_legal_move_is_applied(manager, make_board):
    """A legal move advances the position, flips the turn, and grows history."""
    board = make_board()
    # Ground the chess fact: e2e4 is legal from the starting position.
    assert board.is_legal(chess.Move.from_uci("e2e4"))

    code, white_token, _black_token = create_two_player_room(manager)
    result = manager.apply_move(code, white_token, "e2", "e4")

    assert result.ok is True
    assert result.error is None
    assert result.state is not None
    assert result.state.turn == "black"
    assert result.state.move_history == ["e4"]
    assert result.game_over is None

    # The authoritative board matches a mirror that played the same move.
    board.push(chess.Move.from_uci("e2e4"))
    room = manager.get_room(code)
    assert room.board.fen() == board.fen()
    assert result.state.fen == board.fen()


def test_illegal_move_is_rejected(manager, make_board):
    """Constraint 12: an illegal move is rejected and the room state is unchanged."""
    board = make_board()
    # Ground the chess fact: e2e5 is illegal from the starting position.
    assert not board.is_legal(chess.Move.from_uci("e2e5"))

    code, white_token, _black_token = create_two_player_room(manager)
    room = manager.get_room(code)
    fen_before = room.board.fen()
    history_before = list(room.move_history)

    result = manager.apply_move(code, white_token, "e2", "e5")

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_move"
    assert result.state is None
    # The authoritative position and history did NOT change.
    assert room.board.fen() == fen_before
    assert room.move_history == history_before
    assert room.board.turn == chess.WHITE


def test_move_out_of_turn_is_rejected(manager):
    """A player moving out of turn is rejected without changing the state."""
    code, _white_token, black_token = create_two_player_room(manager)
    room = manager.get_room(code)
    # From the start it is White's turn, so Black moving first is out of turn.
    result = manager.apply_move(code, black_token, "e7", "e5")

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "not_your_turn"
    assert len(room.move_history) == 0
    assert room.board.turn == chess.WHITE


def test_checkmate_ends_game_with_winner(manager, make_board):
    """Fool's mate ends the game with result ``checkmate`` and Black the winner."""
    code, white_token, black_token = create_two_player_room(manager)
    board = make_board()
    result = _play_sans(manager, code, white_token, black_token, board, _FOOLS_MATE_SANS)

    assert result.ok is True
    assert result.game_over is not None
    assert result.game_over.result == "checkmate"
    assert result.game_over.winner == "black"
    # The authoritative position is the verified Fool's-mate mate.
    room = manager.get_room(code)
    assert room.board.fen() == _FOOLS_MATE_FEN
    assert room.status == "finished"
    assert room.winner == "black"


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------
def test_build_state_contains_core_fields(manager, make_board):
    """``build_state`` exposes the live FEN, side to move, history, and slots."""
    code, white_token, black_token = create_two_player_room(manager)
    board = make_board()
    _play_sans(manager, code, white_token, black_token, board, ["e4", "e5"])

    room = manager.get_room(code)
    state = manager.build_state(room)

    assert state.type == "state"
    assert state.turn == "white"
    assert state.move_history == ["e4", "e5"]
    assert state.status == "active"
    # The snapshot FEN matches the mirror and is a well-formed, round-tripping FEN.
    assert state.fen == board.fen()
    assert chess.Board(state.fen).fen() == state.fen
    # The manager tracks two occupied player seats.
    assert room.slots["white"] is not None
    assert room.slots["black"] is not None
    # last_move is tolerant: accept either key set (from_square/to_square OR
    # from/to), or skip the sub-assertion if the snapshot omits it.
    last_move = state.last_move
    if last_move is not None:
        assert ("from_square" in last_move and "to_square" in last_move) or (
            "from" in last_move and "to" in last_move
        )


# ---------------------------------------------------------------------------
# Reconnect (FEN + history replay)
# ---------------------------------------------------------------------------
async def test_reconnect_returns_current_state(manager, make_board):
    """Reconnect returns the full current state (FEN + history) for replay."""
    code, white_token, black_token = create_two_player_room(manager)
    board = make_board()
    _play_sans(manager, code, white_token, black_token, board, ["e4", "e5", "Nf3"])

    # Black drops, then reconnects before the timeout, restoring the live state.
    manager.mark_disconnected(code, black_token)
    state = manager.reconnect(code, black_token)
    await asyncio.sleep(0)  # let the cancelled disconnect timer settle cleanly

    assert state.type == "state"
    assert state.fen == board.fen()
    assert len(state.move_history) == 3
    # The game is still live -- reconnect happened before any forfeit.
    room = manager.get_room(code)
    assert room.status == "active"
    assert room.winner is None


# ---------------------------------------------------------------------------
# Disconnect timeout -> forfeit
# ---------------------------------------------------------------------------
async def test_disconnect_timeout_forfeits_to_opponent():
    """A player who never reconnects forfeits to the opponent after the timeout."""
    manager = make_manager(timeout_s=_FAST_TIMEOUT_S)
    code, white_token, _black_token = create_two_player_room(manager)

    manager.mark_disconnected(code, white_token)
    # Wait well past the tiny timeout (~3x) so the asyncio timer fires.
    await asyncio.sleep(_FAST_TIMEOUT_S * 3)

    room = manager.get_room(code)
    assert room.status == "finished"
    assert room.winner == "black"
    assert room.result == "timeout"


async def test_reconnect_before_timeout_cancels_forfeit():
    """Reconnecting before the deadline cancels the pending forfeit timer."""
    manager = make_manager(timeout_s=_FAST_TIMEOUT_S)
    code, white_token, _black_token = create_two_player_room(manager)

    manager.mark_disconnected(code, white_token)
    state = manager.reconnect(code, white_token)
    assert state.type == "state"
    # Wait past the timeout; because the timer was cancelled, no forfeit fires.
    await asyncio.sleep(_FAST_TIMEOUT_S * 3)

    room = manager.get_room(code)
    assert room.status == "active"
    assert room.winner is None


# ---------------------------------------------------------------------------
# Resignation and forfeit (synchronous)
# ---------------------------------------------------------------------------
def test_resign_awards_win_to_opponent(manager):
    """Resigning ends the game with the opponent recorded as the winner."""
    code, white_token, _black_token = create_two_player_room(manager)
    game_over = manager.resign(code, white_token)

    assert game_over.type == "game_over"
    assert game_over.result == "resignation"
    assert game_over.winner == "black"
    room = manager.get_room(code)
    assert room.status == "finished"
    assert room.winner == "black"


def test_forfeit_is_synchronous_and_awards_opponent(manager):
    """The synchronous, server-driven forfeit awards the win to the opponent."""
    code, _white_token, _black_token = create_two_player_room(manager)
    # The manager realizes forfeit as the internal, color-based ``_forfeit``
    # helper (server-driven, e.g. by the disconnect timer); it is synchronous.
    game_over = manager._forfeit(code, "white")

    assert game_over is not None
    assert game_over.type == "game_over"
    assert game_over.winner == "black"
    room = manager.get_room(code)
    assert room.status == "finished"
    assert room.winner == "black"
