"""Canonical WebSocket message contract for the blitzy-chess backend.

This module is the single source of truth for every JSON message exchanged
over the ``/ws/multiplayer`` and ``/ws/game`` WebSocket endpoints. The API
layer (``chess_ai.api``), the room manager (``chess_ai.rooms.manager``), the
test suites, and the frontend ``frontend/src/types/index.ts`` all mirror the
shapes defined here.

Wire format
-----------
- Serialization is ``json.dumps(dataclasses.asdict(message))``. The JSON keys
  are exactly the dataclass field names, in ``snake_case``. There is no
  camelCase conversion.
- Every message carries a ``type`` string discriminator with a fixed literal
  per class (``"move"``, ``"state"``, ``"ai_thinking"``, ``"game_over"``,
  ``"create_room"``, ``"room_created"``, ``"join_room"``, ``"room_joined"``,
  ``"reconnect"``, ``"resign"``, ``"error"``).
- Colors and turn are the full strings ``"white"`` / ``"black"`` (or ``None``
  for "no winner").
- Squares are lowercase algebraic strings such as ``"e2"`` or ``"g8"``.
  Promotion is a lowercase piece letter ``"q"``, ``"r"``, ``"b"``, ``"n"``, or
  ``None``.
- Move squares use the field names ``from_square`` and ``to_square`` (``from``
  is a Python keyword and is never used as a field name). The frontend mirrors
  ``from_square`` / ``to_square`` end to end.

Purity
------
Pure data only. This module imports the Python standard library
(``dataclasses``, ``json``, ``enum``) and nothing else. It does not import
``chess`` (python-chess), FastAPI, Starlette, or ``chess_ai.config``, so it is
dependency-free and instant to import from any layer.
"""

import json
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum


# ---------------------------------------------------------------------------
# Error codes (closed set)
# ---------------------------------------------------------------------------
class ErrorCode(StrEnum):
    """Closed set of ``ErrorMessage.code`` values.

    Each member is a ``str`` that compares equal to its value and serializes to
    that plain string through ``json``.
    """

    ILLEGAL_MOVE = "illegal_move"
    NOT_YOUR_TURN = "not_your_turn"
    ROOM_NOT_FOUND = "room_not_found"
    ROOM_FULL = "room_full"
    INVALID_MESSAGE = "invalid_message"
    GAME_NOT_ACTIVE = "game_not_active"
    RECONNECT_FAILED = "reconnect_failed"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ProtocolError(Exception):
    """Raised when an inbound message cannot be parsed into a known type.

    The transport catches this and replies with an ``ErrorMessage`` carrying
    ``ErrorCode.INVALID_MESSAGE``.
    """


# ---------------------------------------------------------------------------
# Serialization base
# ---------------------------------------------------------------------------
class _Message:
    """Base for every protocol message.

    Subclasses are ``@dataclass`` definitions whose fields are JSON-primitive
    types. This base supplies the shared serialization helpers.
    """

    def to_dict(self) -> dict[str, object]:
        """Return the message as a plain dict with ``snake_case`` keys."""
        return asdict(self)

    def to_json(self) -> str:
        """Return the message as a JSON string."""
        return json.dumps(self.to_dict())


# ===========================================================================
# Client -> Server (inbound) messages
# ===========================================================================
@dataclass
class CreateRoomMessage(_Message):
    """Request to create a new multiplayer room. No payload fields."""

    type: str = field(default="create_room")


@dataclass
class JoinRoomMessage(_Message):
    """Request to join an existing room by its 6-character code."""

    code: str
    type: str = field(default="join_room")


@dataclass
class MoveMessage(_Message):
    """A move submitted by a client.

    The server reconstructs and validates the move from ``from_square``,
    ``to_square``, and ``promotion``; it never trusts ``san``, which is carried
    only for debugging and telemetry.
    """

    from_square: str
    to_square: str
    promotion: str | None = None
    san: str | None = None
    type: str = field(default="move")


@dataclass
class ReconnectMessage(_Message):
    """Request to restore a player into an existing room.

    ``player_token`` is the token the client received from ``room_created`` or
    ``room_joined``.
    """

    code: str
    player_token: str
    type: str = field(default="reconnect")


@dataclass
class ResignMessage(_Message):
    """Request to resign the current game.

    The connection identifies the player; ``player_token`` is optional and
    used only when the transport prefers explicit identification.
    """

    player_token: str | None = None
    type: str = field(default="resign")


# ===========================================================================
# Server -> Client (outbound) messages
# ===========================================================================
@dataclass
class RoomCreatedMessage(_Message):
    """Response to ``create_room``.

    ``color`` is the creator's assigned color (``"white"``). ``player_token``
    identifies the creator on reconnect.
    """

    code: str
    color: str
    player_token: str
    type: str = field(default="room_created")


@dataclass
class RoomJoinedMessage(_Message):
    """Response to ``join_room``.

    ``color`` is the joiner's assigned color (``"black"``). ``player_token``
    identifies the joiner on reconnect.
    """

    code: str
    color: str
    player_token: str
    type: str = field(default="room_joined")


@dataclass
class StateMessage(_Message):
    """Authoritative position snapshot.

    Fields:
        fen: Board FEN (the authoritative position).
        move_history: SAN strings in order, e.g. ``["e4", "e5", "Nf3"]``. The
            frontend pairs them into numbered rows.
        turn: ``"white"`` or ``"black"`` (whose move it is).
        status: ``"waiting"``, ``"active"``, or ``"finished"``.
        in_check: Whether the side to move is in check.
        last_move: ``{"from_square": str, "to_square": str}`` of the most
            recent move, or ``None`` if no moves have been made.
        winner: ``"white"``, ``"black"``, or ``None``. Terminal info is carried
            primarily by ``GameOverMessage``; these optionals are available for
            snapshots that also report a result.
        result: Terminal result string, or ``None`` for a live position.
    """

    fen: str
    move_history: list[str]
    turn: str
    status: str
    in_check: bool
    last_move: dict[str, str] | None = None
    winner: str | None = None
    result: str | None = None
    type: str = field(default="state")


@dataclass
class AiThinkingMessage(_Message):
    """AI search-progress update, shared by ``/ws/game`` and ``/ws/multiplayer``.

    Mirrors the engine's search info in wire form.

    Fields:
        depth: Completed search depth.
        evaluation: Score in centipawns from White's point of view (positive
            means White is better). The API layer converts the engine's
            side-to-move score to White's POV before building this message.
        pv: Principal variation as SAN move strings, e.g. ``["Nf3", "Nc6"]``.
        nodes: Number of nodes searched.
        time_s: Elapsed search time in seconds, or ``None``.
        nps: Nodes per second, or ``None``.
        mate_in: Moves to forced mate (for an "M3" readout), or ``None``.
        seldepth: Selective (maximum) search depth reached, or ``None``.
    """

    depth: int
    evaluation: int
    pv: list[str]
    nodes: int
    time_s: float | None = None
    nps: int | None = None
    mate_in: int | None = None
    seldepth: int | None = None
    type: str = field(default="ai_thinking")


@dataclass
class GameOverMessage(_Message):
    """End-of-game notification.

    Fields:
        result: One of ``"checkmate"``, ``"stalemate"``, ``"draw"``,
            ``"resignation"``, or ``"timeout"``.
        winner: ``"white"``, ``"black"``, or ``None`` for draws and stalemate.
        reason: Short human-readable explanation, e.g. "Black wins by
            checkmate".
    """

    result: str
    winner: str | None
    reason: str
    type: str = field(default="game_over")


@dataclass
class ErrorMessage(_Message):
    """Error notification.

    ``code`` is drawn from :class:`ErrorCode`; it may be an ``ErrorCode``
    member or its plain string value, both of which serialize to the value.
    """

    code: str
    message: str
    type: str = field(default="error")


# ===========================================================================
# Type registries and (de)serialization helpers
# ===========================================================================
# Inbound types accepted from clients by parse_client_message().
_CLIENT_MESSAGE_TYPES: dict[str, type[_Message]] = {
    "create_room": CreateRoomMessage,
    "join_room": JoinRoomMessage,
    "move": MoveMessage,
    "reconnect": ReconnectMessage,
    "resign": ResignMessage,
}

# Every type, inbound and outbound, used by parse() for round-tripping.
_ALL_MESSAGE_TYPES: dict[str, type[_Message]] = {
    **_CLIENT_MESSAGE_TYPES,
    "room_created": RoomCreatedMessage,
    "room_joined": RoomJoinedMessage,
    "state": StateMessage,
    "ai_thinking": AiThinkingMessage,
    "game_over": GameOverMessage,
    "error": ErrorMessage,
}


def serialize(message: _Message) -> str:
    """Serialize a message to a JSON string for transport."""
    return json.dumps(asdict(message))


def _coerce_payload(raw: str | dict) -> dict:
    """Return a decoded dict payload from a JSON string or an existing dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProtocolError(f"invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ProtocolError("decoded message is not a JSON object")
        return decoded
    raise ProtocolError(f"unsupported message type: {type(raw).__name__}")


def _build(cls: type[_Message], payload: dict) -> _Message:
    """Construct ``cls`` from ``payload``, ignoring unknown keys.

    The ``type`` discriminator is dropped so the class default is used. Missing
    required fields surface as a :class:`ProtocolError`.
    """
    field_names = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in payload.items() if k in field_names and k != "type"}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ProtocolError(f"malformed {payload.get('type')!r} message: {exc}") from exc


def _dispatch(raw: str | dict, registry: dict[str, type[_Message]]) -> _Message:
    """Decode ``raw`` and build the message class named by its discriminator."""
    payload = _coerce_payload(raw)
    msg_type = payload.get("type")
    if not isinstance(msg_type, str):
        raise ProtocolError("missing or non-string 'type' discriminator")
    cls = registry.get(msg_type)
    if cls is None:
        raise ProtocolError(f"unknown message type: {msg_type!r}")
    return _build(cls, payload)


def parse_client_message(raw: str | dict) -> _Message:
    """Parse an inbound client message into its dataclass.

    Accepts a JSON string or an already-decoded dict and returns one of
    :class:`CreateRoomMessage`, :class:`JoinRoomMessage`, :class:`MoveMessage`,
    :class:`ReconnectMessage`, or :class:`ResignMessage`. Unknown keys are
    ignored.

    Raises:
        ProtocolError: If the JSON is invalid, the ``type`` is missing or
            unknown, or a required field is absent.
    """
    return _dispatch(raw, _CLIENT_MESSAGE_TYPES)


def parse(raw: str | dict) -> _Message:
    """Parse any message (inbound or outbound) into its dataclass.

    Primarily used by tests to round-trip serialized messages. Follows the same
    rules and error handling as :func:`parse_client_message`.
    """
    return _dispatch(raw, _ALL_MESSAGE_TYPES)


__all__ = [
    "AiThinkingMessage",
    "CreateRoomMessage",
    "ErrorCode",
    "ErrorMessage",
    "GameOverMessage",
    "JoinRoomMessage",
    "MoveMessage",
    "ProtocolError",
    "ReconnectMessage",
    "ResignMessage",
    "RoomCreatedMessage",
    "RoomJoinedMessage",
    "StateMessage",
    "parse",
    "parse_client_message",
    "serialize",
]
