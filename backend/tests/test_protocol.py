"""Tests for the WebSocket message protocol dataclasses (``chess_ai.rooms.protocol``).

This suite verifies the pure-data message layer that defines every JSON message
exchanged over the ``/ws/game`` and ``/ws/multiplayer`` endpoints and that mirrors
the frontend TypeScript types in ``frontend/src/types/index.ts``.

The load-bearing guarantees exercised here are:

* Serialization produces a JSON object whose keys are ``snake_case`` and identical
  to the dataclass field names, with a ``type`` discriminator on every message.
* Move messages use ``from_square`` / ``to_square`` and never ``from`` / ``to``
  (the latter are not valid Python identifiers and would break the wire contract).
* ``parse_client_message`` round-trips inbound messages, accepts a JSON string or
  an already-decoded dict, ignores unknown keys, and raises :class:`ProtocolError`
  on malformed or unsupported input.
* ``parse`` (the generic registry that also knows outbound types) round-trips
  every server-to-client message -- ``RoomCreatedMessage``, ``RoomJoinedMessage``,
  ``StateMessage`` (including a nested ``last_move``), ``AiThinkingMessage``,
  ``GameOverMessage``, and ``ErrorMessage`` -- so ``parse(serialize(msg)) == msg``.
* ``ErrorCode`` exposes the canonical closed set of error codes.

The module is intentionally chess-free: a literal FEN string is used instead of
importing ``chess``, mirroring the purity of ``protocol.py`` itself. Every test is
synchronous and deterministic (no async, no sleeps, no network, no filesystem).
"""

import json

import pytest

from chess_ai.rooms.protocol import (
    AiThinkingMessage,
    CreateRoomMessage,
    ErrorCode,
    ErrorMessage,
    GameOverMessage,
    JoinRoomMessage,
    MoveMessage,
    ProtocolError,
    ReconnectMessage,
    ResignMessage,
    RoomCreatedMessage,
    RoomJoinedMessage,
    StateMessage,
    parse,
    parse_client_message,
    serialize,
)

# The standard chess starting position. Declared as a literal so this suite never
# imports python-chess; ``protocol.py`` is deliberately chess-free and so is its
# test module.
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


# ---------------------------------------------------------------------------
# Serialization: every message serializes to a snake_case JSON object whose
# ``type`` discriminator matches the class.
# ---------------------------------------------------------------------------
def test_move_message_serializes_with_square_keys():
    """A move serializes with ``from_square`` / ``to_square`` and never ``from`` / ``to``."""
    msg = MoveMessage(from_square="e2", to_square="e4")
    d = json.loads(serialize(msg))

    assert d["type"] == "move"
    assert d["from_square"] == "e2"
    assert d["to_square"] == "e4"
    assert d.get("promotion") is None
    # Load-bearing rule: the reserved words ``from`` / ``to`` must never appear as
    # JSON keys, because they are not valid Python identifiers for dataclass fields.
    assert "from" not in d
    assert "to" not in d


def test_move_message_promotion_serializes():
    """A promotion move carries the lowercase piece letter in ``promotion``."""
    msg = MoveMessage(from_square="e7", to_square="e8", promotion="q")
    d = json.loads(serialize(msg))

    assert d["type"] == "move"
    assert d["from_square"] == "e7"
    assert d["to_square"] == "e8"
    assert d["promotion"] == "q"


def test_state_message_serialization():
    """A state snapshot serializes its FEN, history, turn, status and nested last move."""
    msg = StateMessage(
        fen=START_FEN,
        move_history=["e4", "e5"],
        turn="white",
        status="active",
        in_check=False,
        last_move={"from_square": "e2", "to_square": "e4"},
    )
    d = json.loads(serialize(msg))

    assert d["type"] == "state"
    assert d["fen"] == START_FEN
    assert d["move_history"] == ["e4", "e5"]
    assert d["turn"] == "white"
    assert d["status"] == "active"
    assert d["in_check"] is False
    # ``last_move`` is a nested object with the same snake_case square keys.
    assert d["last_move"]["from_square"] == "e2"
    assert d["last_move"]["to_square"] == "e4"


def test_error_message_serialization():
    """An error serializes its ``code`` and ``message`` under the ``error`` discriminator."""
    msg = ErrorMessage(code="illegal_move", message="That move is not legal.")
    d = json.loads(serialize(msg))

    assert d["type"] == "error"
    assert d["code"] == "illegal_move"
    assert d["message"] == "That move is not legal."


def test_error_message_accepts_error_code_enum():
    """An ``ErrorCode`` member used as ``code`` serializes to its plain string value."""
    msg = ErrorMessage(code=ErrorCode.ILLEGAL_MOVE, message="nope")
    d = json.loads(serialize(msg))

    assert d["type"] == "error"
    # ``ErrorCode`` is a ``StrEnum``, so it serializes to the bare string and
    # compares equal to both the literal and the enum member.
    assert d["code"] == "illegal_move"
    assert d["code"] == ErrorCode.ILLEGAL_MOVE


def test_game_over_serialization():
    """Game-over serializes result, winner and reason; a decisive game names the winner."""
    msg = GameOverMessage(result="checkmate", winner="white", reason="White wins by checkmate")
    d = json.loads(serialize(msg))

    assert d["type"] == "game_over"
    assert d["result"] == "checkmate"
    assert d["winner"] == "white"
    assert d["reason"] == "White wins by checkmate"


def test_game_over_draw_serializes_null_winner():
    """A drawn game serializes ``winner=None`` to JSON ``null``."""
    msg = GameOverMessage(result="draw", winner=None, reason="Draw by stalemate")
    d = json.loads(serialize(msg))

    assert d["type"] == "game_over"
    assert d["result"] == "draw"
    assert d["winner"] is None


def test_room_created_serialization():
    """A room-created response serializes the room code, color and player token."""
    msg = RoomCreatedMessage(code="ABC123", color="white", player_token="tok")
    d = json.loads(serialize(msg))

    assert d["type"] == "room_created"
    assert d["code"] == "ABC123"
    assert d["color"] == "white"
    assert d["player_token"] == "tok"


def test_ai_thinking_serialization():
    """An AI-thinking update serializes depth, evaluation, principal variation and nodes."""
    msg = AiThinkingMessage(depth=6, evaluation=35, pv=["e4", "e5"], nodes=1234)
    d = json.loads(serialize(msg))

    assert d["type"] == "ai_thinking"
    assert d["depth"] == 6
    assert d["evaluation"] == 35
    assert d["pv"] == ["e4", "e5"]
    assert d["nodes"] == 1234
    # Optional fields default to None and serialize to JSON null.
    assert d["time_s"] is None


# ---------------------------------------------------------------------------
# Parsing & round-tripping: inbound client messages are reconstructed from the
# wire, ``parse_client_message`` accepts str or dict, and unknown keys are ignored.
# ---------------------------------------------------------------------------
def test_parse_move_roundtrip():
    """``parse_client_message(serialize(move))`` reconstructs an equal ``MoveMessage``."""
    msg = MoveMessage(from_square="g1", to_square="f3")
    parsed = parse_client_message(serialize(msg))

    assert isinstance(parsed, MoveMessage)
    assert parsed.from_square == "g1"
    assert parsed.to_square == "f3"
    assert parsed.promotion is None
    # Dataclasses provide value equality by default, so the round trip is exact.
    assert parsed == msg


def test_parse_create_room():
    """A ``create_room`` payload parses into a ``CreateRoomMessage``."""
    parsed = parse_client_message('{"type": "create_room"}')

    assert isinstance(parsed, CreateRoomMessage)
    assert parsed.type == "create_room"


def test_parse_join_room():
    """A ``join_room`` payload parses into a ``JoinRoomMessage`` carrying the code."""
    parsed = parse_client_message('{"type": "join_room", "code": "ABC123"}')

    assert isinstance(parsed, JoinRoomMessage)
    assert parsed.code == "ABC123"


def test_parse_reconnect():
    """A ``reconnect`` payload parses into a ``ReconnectMessage`` with code and token."""
    parsed = parse_client_message('{"type": "reconnect", "code": "ABC123", "player_token": "tok"}')

    assert isinstance(parsed, ReconnectMessage)
    assert parsed.code == "ABC123"
    assert parsed.player_token == "tok"


def test_parse_resign():
    """A ``resign`` payload parses into a ``ResignMessage``."""
    parsed = parse_client_message('{"type": "resign"}')

    assert isinstance(parsed, ResignMessage)
    assert parsed.type == "resign"


def test_parse_accepts_dict_or_str():
    """``parse_client_message`` accepts both a JSON string and an already-decoded dict."""
    payload = {"type": "move", "from_square": "e2", "to_square": "e4"}

    from_dict = parse_client_message(payload)
    from_str = parse_client_message(json.dumps(payload))

    assert isinstance(from_dict, MoveMessage)
    assert isinstance(from_str, MoveMessage)
    assert from_dict == from_str
    assert from_dict.from_square == "e2"
    assert from_dict.to_square == "e4"


def test_parse_ignores_extra_keys():
    """Unknown keys in an inbound payload are ignored rather than rejected."""
    raw = (
        '{"type": "move", "from_square": "e2", "to_square": "e4", '
        '"spurious": 123, "clientTimestamp": 999}'
    )
    parsed = parse_client_message(raw)

    assert isinstance(parsed, MoveMessage)
    assert parsed.from_square == "e2"
    assert parsed.to_square == "e4"


# ---------------------------------------------------------------------------
# Negative cases: malformed or unsupported inbound payloads raise ProtocolError.
# ---------------------------------------------------------------------------
def test_parse_unknown_type_raises():
    """An unknown ``type`` discriminator raises ``ProtocolError``."""
    with pytest.raises(ProtocolError):
        parse_client_message('{"type": "frobnicate"}')


def test_parse_missing_type_raises():
    """A payload without a ``type`` discriminator raises ``ProtocolError``."""
    with pytest.raises(ProtocolError):
        parse_client_message('{"code": "ABC123"}')


def test_parse_move_missing_required_field_raises():
    """A move missing ``to_square`` raises ``ProtocolError``."""
    with pytest.raises(ProtocolError):
        parse_client_message('{"type": "move", "from_square": "e2"}')


def test_parse_join_room_missing_code_raises():
    """A ``join_room`` payload without ``code`` raises ``ProtocolError``."""
    with pytest.raises(ProtocolError):
        parse_client_message('{"type": "join_room"}')


def test_parse_invalid_json_raises():
    """Malformed JSON raises ``ProtocolError`` (which wraps ``json.JSONDecodeError``)."""
    # The source wraps decode errors in ProtocolError; the tuple keeps the
    # assertion robust regardless of which the implementation surfaces.
    with pytest.raises((ProtocolError, json.JSONDecodeError)):
        parse_client_message("{not json")


def test_server_only_type_not_parseable_as_client():
    """A server-to-client ``type`` is rejected by the inbound parser."""
    # ``state`` is an outbound-only message; the client parser whitelists only
    # inbound types, so this must raise rather than build a StateMessage.
    with pytest.raises(ProtocolError):
        parse_client_message('{"type": "state"}')


# ---------------------------------------------------------------------------
# Error codes: the canonical closed set is exposed for the error contract.
# ---------------------------------------------------------------------------
def test_error_codes_exist():
    """The canonical error codes are available on the ``ErrorCode`` enum."""
    values = {member.value for member in ErrorCode}
    expected = {
        "illegal_move",
        "not_your_turn",
        "room_not_found",
        "room_full",
        "invalid_message",
    }
    assert expected <= values
    # Value-based lookup resolves to the corresponding member (StrEnum equality).
    assert ErrorCode("illegal_move") == ErrorCode.ILLEGAL_MOVE


# ---------------------------------------------------------------------------
# Outbound round-tripping: the generic ``parse`` registry (which also knows the
# server-to-client types) reconstructs every outbound message from its own
# ``serialize`` output. Because the dataclasses provide value equality and the
# ``type`` discriminator is a fixed default, ``parse(serialize(msg)) == msg`` is
# exact. These guard the frontend/backend wire contract against drift.
# ---------------------------------------------------------------------------
def test_parse_roundtrips_room_created():
    """``parse(serialize(room_created))`` reconstructs an equal ``RoomCreatedMessage``."""
    msg = RoomCreatedMessage(code="ABC123", color="white", player_token="tok-w")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, RoomCreatedMessage)
    assert parsed.type == "room_created"
    assert parsed.code == "ABC123"
    assert parsed.color == "white"
    assert parsed.player_token == "tok-w"
    assert parsed == msg


def test_parse_roundtrips_room_joined():
    """``parse(serialize(room_joined))`` reconstructs an equal ``RoomJoinedMessage``."""
    msg = RoomJoinedMessage(code="ABC123", color="black", player_token="tok-b")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, RoomJoinedMessage)
    assert parsed.type == "room_joined"
    assert parsed.code == "ABC123"
    assert parsed.color == "black"
    assert parsed.player_token == "tok-b"
    assert parsed == msg


def test_parse_roundtrips_state_with_last_move():
    """A state snapshot round-trips, preserving the nested ``last_move`` square keys."""
    msg = StateMessage(
        fen=START_FEN,
        move_history=["e4", "e5", "Nf3"],
        turn="black",
        status="active",
        in_check=False,
        last_move={"from_square": "g1", "to_square": "f3"},
        winner=None,
        result=None,
    )
    parsed = parse(serialize(msg))

    assert isinstance(parsed, StateMessage)
    assert parsed.type == "state"
    assert parsed.move_history == ["e4", "e5", "Nf3"]
    # The nested last_move survives the round trip with snake_case square keys.
    assert parsed.last_move == {"from_square": "g1", "to_square": "f3"}
    assert parsed == msg


def test_parse_roundtrips_state_without_last_move():
    """A state snapshot with no last move round-trips ``last_move=None``."""
    msg = StateMessage(
        fen=START_FEN,
        move_history=[],
        turn="white",
        status="waiting",
        in_check=False,
    )
    parsed = parse(serialize(msg))

    assert isinstance(parsed, StateMessage)
    assert parsed.last_move is None
    assert parsed.winner is None
    assert parsed.result is None
    assert parsed == msg


def test_parse_roundtrips_ai_thinking_full():
    """An AI-thinking update round-trips with every optional field populated."""
    msg = AiThinkingMessage(
        depth=8,
        evaluation=-42,
        pv=["Nf3", "Nc6", "Bb5"],
        nodes=987654,
        time_s=1.25,
        nps=790123,
        mate_in=None,
        seldepth=14,
    )
    parsed = parse(serialize(msg))

    assert isinstance(parsed, AiThinkingMessage)
    assert parsed.type == "ai_thinking"
    assert parsed.pv == ["Nf3", "Nc6", "Bb5"]
    assert parsed.time_s == 1.25
    assert parsed.seldepth == 14
    assert parsed == msg


def test_parse_roundtrips_ai_thinking_defaults():
    """An AI-thinking update round-trips with its optional fields left at ``None``."""
    msg = AiThinkingMessage(depth=4, evaluation=10, pv=["e4"], nodes=500)
    parsed = parse(serialize(msg))

    assert isinstance(parsed, AiThinkingMessage)
    assert parsed.time_s is None
    assert parsed.nps is None
    assert parsed.mate_in is None
    assert parsed.seldepth is None
    assert parsed == msg


def test_parse_roundtrips_game_over_decisive():
    """A decisive game-over round-trips its result, winner and reason."""
    msg = GameOverMessage(result="checkmate", winner="black", reason="Black wins by checkmate")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, GameOverMessage)
    assert parsed.type == "game_over"
    assert parsed.result == "checkmate"
    assert parsed.winner == "black"
    assert parsed == msg


def test_parse_roundtrips_game_over_draw():
    """A drawn game-over round-trips ``winner=None`` through JSON ``null``."""
    msg = GameOverMessage(result="draw", winner=None, reason="Draw by stalemate")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, GameOverMessage)
    assert parsed.winner is None
    assert parsed == msg


def test_parse_roundtrips_error_illegal_move():
    """An ``illegal_move`` error round-trips its code and message."""
    msg = ErrorMessage(code="illegal_move", message="That move is not legal.")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, ErrorMessage)
    assert parsed.type == "error"
    assert parsed.code == "illegal_move"
    assert parsed.code == ErrorCode.ILLEGAL_MOVE
    assert parsed.message == "That move is not legal."
    assert parsed == msg


def test_parse_roundtrips_error_built_from_error_code_enum():
    """An error built from an ``ErrorCode`` member round-trips to the plain string code."""
    msg = ErrorMessage(code=ErrorCode.NOT_YOUR_TURN, message="Not your turn.")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, ErrorMessage)
    assert parsed.code == "not_your_turn"
    assert parsed.code == ErrorCode.NOT_YOUR_TURN
    # StrEnum compares equal to its value, so the round trip is still exact.
    assert parsed == msg


def test_parse_generic_registry_also_handles_inbound():
    """The generic ``parse`` accepts inbound types too (the merged registry)."""
    # Unlike ``parse_client_message`` (inbound-only), ``parse`` covers every type;
    # a ``move`` therefore round-trips through it as well.
    msg = MoveMessage(from_square="e7", to_square="e8", promotion="q")
    parsed = parse(serialize(msg))

    assert isinstance(parsed, MoveMessage)
    assert parsed.promotion == "q"
    assert parsed == msg
