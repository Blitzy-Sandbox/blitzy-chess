"""Multiplayer room registry and lifecycle for the blitzy-chess backend.

This module owns all state for real-time two-human games: room codes, the two
player slots, the authoritative ``chess.Board`` per room, SAN move history, the
disconnect timer, reconnect replay, and resignation/forfeit resolution.

It is transport-agnostic. It manages state only and imports no web framework;
the WebSocket handler (``chess_ai.api.multiplayer_ws``) is a thin adapter that
calls these methods and performs the socket I/O. To notify a player outside a
direct request/response (a timeout forfeit), the transport registers an opaque
async "send" callable per slot; the manager treats it as a black box.

Authority and validation:
    The ``chess.Board`` stored in each room is the single source of truth for
    the position. Every inbound move is validated with ``board.is_legal`` before
    it is applied; illegal moves are rejected as an ``ErrorMessage`` payload.

Concurrency:
    The state-mutation methods (``create_room``, ``join_room``, ``apply_move``,
    ``reconnect``, ``resign``, ``get_room``, ``remove_room``) are synchronous and
    need no event loop. Player-initiated resignation (``resign``) requires the
    player's issued token; color-based forfeit is the internal ``_forfeit``
    helper, driven only by the disconnect timer with a server-derived color, so a
    client-controlled color string can never identify (and end the game for) a
    player. Only the disconnect timer (``mark_disconnected`` scheduling,
    ``_disconnect_timeout``, and ``broadcast``) uses ``asyncio``. The timeout is
    injectable per manager instance so tests run fast and deterministically.

Returned objects are the canonical ``chess_ai.rooms.protocol`` dataclasses so
the transport only has to serialize them and the frontend types stay in step.
"""

import asyncio
import logging
import secrets
import string
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import chess

from chess_ai.rooms import protocol

logger = logging.getLogger(__name__)

# Number of characters in a shareable room code.
ROOM_CODE_LENGTH = 6

# Default seconds a player may stay disconnected before forfeiting. Overridable
# per RoomManager instance via the constructor.
DISCONNECT_TIMEOUT_S = 60.0

# Uppercase letters and digits, with the visually ambiguous characters O, 0, I,
# and 1 removed so codes are easy to read aloud and type.
_CODE_ALPHABET = "".join(
    char for char in (string.ascii_uppercase + string.digits) if char not in "O0I1"
)

# Wire color strings (match protocol.py and the frontend types).
_COLOR_WHITE = "white"
_COLOR_BLACK = "black"

# Room lifecycle states.
_STATUS_WAITING = "waiting"
_STATUS_ACTIVE = "active"
_STATUS_FINISHED = "finished"


def color_name(turn: bool) -> str:
    """Return the wire color string for a python-chess turn flag.

    Args:
        turn: ``chess.WHITE`` (``True``) or ``chess.BLACK`` (``False``).

    Returns:
        ``"white"`` for ``chess.WHITE``, otherwise ``"black"``.
    """
    return _COLOR_WHITE if turn == chess.WHITE else _COLOR_BLACK


def opponent(color: str) -> str:
    """Return the opposing wire color string."""
    return _COLOR_BLACK if color == _COLOR_WHITE else _COLOR_WHITE


def _result_reason(result: str, winner: str | None) -> str:
    """Return a short human-readable reason for a terminal result."""
    if winner is not None:
        phrase = {
            "checkmate": "wins by checkmate",
            "resignation": "wins by resignation",
            "timeout": "wins on time",
        }.get(result, f"wins ({result})")
        return f"{winner.capitalize()} {phrase}"
    if result == "stalemate":
        return "Draw by stalemate"
    return "Draw"


@dataclass
class PlayerSlot:
    """One seat in a room.

    Attributes:
        color: ``"white"`` or ``"black"``.
        player_token: Opaque token issued on create/join, used to authenticate
            reconnects, moves, and resignations.
        connected: Whether the transport currently holds a live socket.
        send: Opaque async callable registered by the transport for pushed
            messages. The manager never inspects it.
        disconnect_task: Pending forfeit timer, cancelled on reconnect.
        disconnect_deadline: Monotonic time at which the forfeit fires.
    """

    color: str
    player_token: str
    connected: bool = True
    send: Callable[[dict], Awaitable[None]] | None = None
    disconnect_task: asyncio.Task | None = None
    disconnect_deadline: float | None = None


@dataclass
class Room:
    """Authoritative state for one multiplayer game.

    Attributes:
        code: The shareable room code.
        board: The authoritative position (a fresh board at creation).
        slots: Color to ``PlayerSlot`` (or ``None`` for an empty seat).
        move_history: SAN strings in play order.
        status: ``"waiting"``, ``"active"``, or ``"finished"``.
        winner: ``"white"``, ``"black"``, or ``None``.
        result: Terminal reason, or ``None`` while the game is live.
        created_at: Wall-clock creation timestamp.
    """

    code: str
    board: chess.Board = field(default_factory=chess.Board)
    slots: dict[str, PlayerSlot | None] = field(
        default_factory=lambda: {_COLOR_WHITE: None, _COLOR_BLACK: None}
    )
    move_history: list[str] = field(default_factory=list)
    status: str = _STATUS_WAITING
    winner: str | None = None
    result: str | None = None
    created_at: float = field(default_factory=time.time)

    def is_full(self) -> bool:
        """Return True when both color slots are occupied."""
        return all(self.slots[color] is not None for color in (_COLOR_WHITE, _COLOR_BLACK))

    def slot_for_token(self, token: str) -> PlayerSlot | None:
        """Return the slot whose ``player_token`` matches ``token``, or None."""
        for slot in self.slots.values():
            if slot is not None and slot.player_token == token:
                return slot
        return None

    def both_connected(self) -> bool:
        """Return True when both seats are occupied and connected."""
        return all(slot is not None and slot.connected for slot in self.slots.values())


@dataclass
class MoveResult:
    """Outcome of :meth:`RoomManager.apply_move`.

    On rejection, ``ok`` is ``False`` and ``error`` carries the reason. On
    acceptance, ``ok`` is ``True``, ``state`` is the new snapshot, and
    ``game_over`` is set only when the move ended the game.
    """

    ok: bool
    error: protocol.ErrorMessage | None = None
    state: protocol.StateMessage | None = None
    game_over: protocol.GameOverMessage | None = None


class RoomManager:
    """In-process registry and lifecycle owner for multiplayer rooms.

    A single instance is shared by the WebSocket transport. It maps room codes
    to :class:`Room` objects and exposes synchronous state methods plus an
    asyncio-based disconnect timer and broadcast helper.
    """

    def __init__(
        self,
        *,
        disconnect_timeout_s: float = DISCONNECT_TIMEOUT_S,
        code_length: int = ROOM_CODE_LENGTH,
    ) -> None:
        """Create an empty registry.

        Args:
            disconnect_timeout_s: Seconds before a disconnected player forfeits.
                Injectable so tests can use a tiny value.
            code_length: Number of characters in a generated room code.
        """
        self._rooms: dict[str, Room] = {}
        self._disconnect_timeout_s = disconnect_timeout_s
        self._code_length = code_length

    # ------------------------------------------------------------------
    # Code generation and registry lookup
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_code(code: str) -> str:
        """Return the lookup form of a user-entered code (stripped, uppercased)."""
        return code.strip().upper()

    def _generate_code(self) -> str:
        """Return a unique code over the uppercase-alnum alphabet."""
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(self._code_length))
            if code not in self._rooms:
                return code

    def get_room(self, code: str) -> Room | None:
        """Return the room registered under ``code`` (normalized), or None."""
        if not isinstance(code, str):
            return None
        return self._rooms.get(self._normalize_code(code))

    def remove_room(self, code: str) -> None:
        """Remove a room and cancel any pending disconnect timers."""
        if not isinstance(code, str):
            return
        room = self._rooms.pop(self._normalize_code(code), None)
        if room is None:
            return
        for slot in room.slots.values():
            if slot is not None and slot.disconnect_task is not None:
                slot.disconnect_task.cancel()
                slot.disconnect_task = None
        logger.info("room removed: %s", room.code)

    # ------------------------------------------------------------------
    # Room creation and joining
    # ------------------------------------------------------------------
    def create_room(self) -> protocol.RoomCreatedMessage:
        """Create a room, seat the creator as white, and return the response."""
        code = self._generate_code()
        room = Room(code=code)
        token = uuid.uuid4().hex
        room.slots[_COLOR_WHITE] = PlayerSlot(
            color=_COLOR_WHITE, player_token=token, connected=True
        )
        room.status = _STATUS_WAITING
        self._rooms[code] = room
        logger.info("room created: %s", code)
        return protocol.RoomCreatedMessage(code=code, color=_COLOR_WHITE, player_token=token)

    def join_room(self, code: str) -> protocol.RoomJoinedMessage | protocol.ErrorMessage:
        """Seat a joiner as black in an existing room and activate the game.

        Returns a :class:`protocol.RoomJoinedMessage` on success, or an
        :class:`protocol.ErrorMessage` with ``room_not_found`` / ``room_full``.
        """
        room = self.get_room(code)
        if room is None:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.ROOM_NOT_FOUND,
                message=f"No room found for code {code!r}.",
            )
        if room.is_full():
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.ROOM_FULL,
                message=f"Room {room.code} already has two players.",
            )
        token = uuid.uuid4().hex
        room.slots[_COLOR_BLACK] = PlayerSlot(
            color=_COLOR_BLACK, player_token=token, connected=True
        )
        room.status = _STATUS_ACTIVE
        logger.info("room joined: %s", room.code)
        return protocol.RoomJoinedMessage(code=room.code, color=_COLOR_BLACK, player_token=token)

    # ------------------------------------------------------------------
    # Move application (server-authoritative)
    # ------------------------------------------------------------------
    def apply_move(
        self,
        code: str,
        player_token: str,
        from_square: str,
        to_square: str,
        promotion: str | None = None,
    ) -> MoveResult:
        """Validate and apply a move to the room's authoritative board.

        The move is rebuilt from ``from_square``/``to_square``/``promotion`` and
        validated with ``board.is_legal`` before it is pushed. Any rejection
        returns ``MoveResult(ok=False, error=...)`` and leaves the board
        unchanged; acceptance returns the new state and an optional terminal
        ``game_over``.
        """
        room = self.get_room(code)
        if room is None:
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.ROOM_NOT_FOUND,
                    message=f"No room found for code {code!r}.",
                ),
            )
        slot = room.slot_for_token(player_token)
        if slot is None:
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.NOT_YOUR_TURN,
                    message="Unknown player token for this room.",
                ),
            )
        if room.status != _STATUS_ACTIVE:
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.GAME_NOT_ACTIVE,
                    message=f"Game is not active (status {room.status!r}).",
                ),
            )
        if color_name(room.board.turn) != slot.color:
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.NOT_YOUR_TURN,
                    message=f"It is {color_name(room.board.turn)}'s turn.",
                ),
            )
        uci = f"{from_square}{to_square}{promotion or ''}"
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.ILLEGAL_MOVE,
                    message=f"Malformed move {uci!r}.",
                ),
            )
        if not room.board.is_legal(move):
            return MoveResult(
                ok=False,
                error=protocol.ErrorMessage(
                    code=protocol.ErrorCode.ILLEGAL_MOVE,
                    message=f"Illegal move {uci!r} in this position.",
                ),
            )
        san = room.board.san(move)
        room.board.push(move)
        room.move_history.append(san)
        game_over = self._detect_terminal(room, mover_color=slot.color)
        logger.info("move applied in %s: %s", room.code, san)
        return MoveResult(ok=True, state=self.build_state(room), game_over=game_over)

    def _detect_terminal(self, room: Room, *, mover_color: str) -> protocol.GameOverMessage | None:
        """Resolve a terminal position after a push, or return None.

        Checks checkmate, stalemate, and the forced draws (insufficient
        material, seventy-five-move rule, fivefold repetition). Claimable draws
        (threefold, fifty-move) are not auto-resolved.
        """
        board = room.board
        if board.is_checkmate():
            return self._finish(room, result="checkmate", winner=mover_color)
        if board.is_stalemate():
            return self._finish(room, result="stalemate", winner=None)
        if (
            board.is_insufficient_material()
            or board.is_seventyfive_moves()
            or board.is_fivefold_repetition()
        ):
            return self._finish(room, result="draw", winner=None)
        return None

    def _finish(self, room: Room, *, result: str, winner: str | None) -> protocol.GameOverMessage:
        """Mark a room finished and return its ``GameOverMessage``."""
        room.status = _STATUS_FINISHED
        room.winner = winner
        room.result = result
        logger.info("game over in %s: %s (winner=%s)", room.code, result, winner)
        return protocol.GameOverMessage(
            result=result, winner=winner, reason=_result_reason(result, winner)
        )

    def build_state(self, room: Room) -> protocol.StateMessage:
        """Return an authoritative :class:`protocol.StateMessage` snapshot."""
        last_move: dict[str, str] | None = None
        if room.board.move_stack:
            last = room.board.peek()
            last_move = {
                "from_square": chess.square_name(last.from_square),
                "to_square": chess.square_name(last.to_square),
            }
        return protocol.StateMessage(
            fen=room.board.fen(),
            move_history=list(room.move_history),
            turn=color_name(room.board.turn),
            status=room.status,
            in_check=room.board.is_check(),
            last_move=last_move,
            winner=room.winner,
            result=room.result,
        )

    # ------------------------------------------------------------------
    # Disconnect timer and reconnect
    # ------------------------------------------------------------------
    def mark_disconnected(self, code: str, player_token: str) -> None:
        """Mark a player disconnected and schedule a forfeit timeout.

        Synchronous. Inside a running event loop it schedules an asyncio timer
        that forfeits the game if the player does not reconnect within the
        configured window; with no running loop it records the disconnect only
        (tests then call :meth:`_forfeit` directly).
        """
        room = self.get_room(code)
        if room is None:
            return
        slot = room.slot_for_token(player_token)
        if slot is None:
            return
        slot.connected = False
        slot.disconnect_deadline = time.monotonic() + self._disconnect_timeout_s
        logger.info("player disconnected in %s: %s", room.code, slot.color)
        try:
            slot.disconnect_task = asyncio.create_task(
                self._disconnect_timeout(room.code, player_token)
            )
        except RuntimeError:
            slot.disconnect_task = None

    async def _disconnect_timeout(self, code: str, player_token: str) -> None:
        """Forfeit the game if the player is still disconnected after the window.

        Cancellation (on reconnect) propagates out of the sleep and ends the
        coroutine without forfeiting.
        """
        await asyncio.sleep(self._disconnect_timeout_s)
        room = self.get_room(code)
        if room is None:
            return
        slot = room.slot_for_token(player_token)
        if slot is None or slot.connected or room.status != _STATUS_ACTIVE:
            return
        game_over = self._forfeit(room.code, slot.color, reason="timeout")
        if game_over is not None:
            await self.broadcast(room, game_over)

    def reconnect(
        self, code: str, player_token: str
    ) -> protocol.StateMessage | protocol.ErrorMessage:
        """Restore a player into a live room and return a replay snapshot.

        Cancels the pending disconnect timer and returns a
        :class:`protocol.StateMessage` (FEN plus full SAN history) so the client
        rebuilds the exact position. Returns an ``ErrorMessage`` if the room is
        gone, the token is unknown, or the game has already finished.
        """
        room = self.get_room(code)
        if room is None:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.ROOM_NOT_FOUND,
                message=f"No room found for code {code!r}.",
            )
        slot = room.slot_for_token(player_token)
        if slot is None:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.RECONNECT_FAILED,
                message="Unknown player token for this room.",
            )
        if room.status == _STATUS_FINISHED:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.RECONNECT_FAILED,
                message="Game has already finished.",
            )
        if slot.disconnect_task is not None:
            slot.disconnect_task.cancel()
            slot.disconnect_task = None
        slot.disconnect_deadline = None
        slot.connected = True
        logger.info("player reconnected in %s: %s", room.code, slot.color)
        return self.build_state(room)

    # ------------------------------------------------------------------
    # Resignation and forfeit
    # ------------------------------------------------------------------
    def resign(
        self, code: str, player_token: str
    ) -> protocol.GameOverMessage | protocol.ErrorMessage:
        """Resign the game for the player identified by ``player_token``.

        Player-initiated resignation is token-authenticated: the caller must
        present the opaque token issued on create/join (the same token that
        authorizes a move). A raw color string is NOT accepted, so possession of
        a room code alone can never resign on another player's behalf. The
        opponent of the token's slot is recorded as the winner. Idempotent on a
        finished room: the existing terminal ``GameOverMessage`` is returned.

        Returns an ``ErrorMessage`` with ``NOT_YOUR_TURN`` when the token does
        not match an occupied slot in the room.
        """
        room = self.get_room(code)
        if room is None:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.ROOM_NOT_FOUND,
                message=f"No room found for code {code!r}.",
            )
        if room.status == _STATUS_FINISHED:
            existing = self._terminal_game_over(room)
            if existing is not None:
                return existing
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.GAME_NOT_ACTIVE,
                message="Game has already finished.",
            )
        slot = room.slot_for_token(player_token)
        if slot is None:
            return protocol.ErrorMessage(
                code=protocol.ErrorCode.NOT_YOUR_TURN,
                message="Unknown player token for this room.",
            )
        logger.info("player resigned in %s: %s", room.code, slot.color)
        return self._finish(room, result="resignation", winner=opponent(slot.color))

    def _forfeit(
        self, code: str, loser_color: str, reason: str = "timeout"
    ) -> protocol.GameOverMessage | None:
        """Finish the game against ``loser_color`` (INTERNAL, color-based).

        This is a private helper for SERVER-DRIVEN terminations only -- the
        disconnect-timeout forfeit -- where ``loser_color`` is derived from an
        authenticated slot, never from client input. It is deliberately not part
        of the public API: a client-controlled color string must never identify a
        player (that is the role of token-authenticated :meth:`resign`).

        ``loser_color`` is validated against the room's occupied slots; an unknown
        color or an empty seat returns ``None`` without mutating state. ``reason``
        is also the terminal result string (default ``"timeout"``). Synchronous
        and directly callable so tests need not wait for the real timer.
        Idempotent: returns the existing terminal message if the room is already
        finished, or ``None`` if the room is gone.
        """
        room = self.get_room(code)
        if room is None:
            return None
        if room.status == _STATUS_FINISHED:
            return self._terminal_game_over(room)
        if loser_color not in (_COLOR_WHITE, _COLOR_BLACK) or room.slots.get(loser_color) is None:
            logger.warning(
                "ignoring forfeit in %s: %r is not an occupied player slot",
                room.code,
                loser_color,
            )
            return None
        logger.info("player forfeited in %s: %s (%s)", room.code, loser_color, reason)
        return self._finish(room, result=reason, winner=opponent(loser_color))

    def _terminal_game_over(self, room: Room) -> protocol.GameOverMessage | None:
        """Rebuild the ``GameOverMessage`` for a finished room, or return None."""
        if room.status != _STATUS_FINISHED or room.result is None:
            return None
        return protocol.GameOverMessage(
            result=room.result,
            winner=room.winner,
            reason=_result_reason(room.result, room.winner),
        )

    # ------------------------------------------------------------------
    # Notification (transport-agnostic)
    # ------------------------------------------------------------------
    def register_connection(
        self, code: str, color: str, send: Callable[[dict], Awaitable[None]]
    ) -> None:
        """Attach a transport send callable to a color slot and mark it connected."""
        room = self.get_room(code)
        if room is None:
            return
        slot = room.slots.get(color)
        if slot is None:
            return
        slot.send = send
        slot.connected = True

    def unregister_connection(self, code: str, color: str) -> None:
        """Detach the transport send callable from a color slot."""
        room = self.get_room(code)
        if room is None:
            return
        slot = room.slots.get(color)
        if slot is None:
            return
        slot.send = None

    async def broadcast(self, room: Room, message: protocol._Message) -> None:
        """Send ``message`` to every connected slot that has a send callable.

        A failed send to one player is logged and does not stop delivery to the
        other.
        """
        payload = message.to_dict()
        for slot in room.slots.values():
            if slot is None or slot.send is None or not slot.connected:
                continue
            try:
                await slot.send(payload)
            except Exception:
                logger.exception("broadcast send failed in %s to %s", room.code, slot.color)


__all__ = [
    "DISCONNECT_TIMEOUT_S",
    "ROOM_CODE_LENGTH",
    "MoveResult",
    "PlayerSlot",
    "Room",
    "RoomManager",
    "color_name",
    "opponent",
]
