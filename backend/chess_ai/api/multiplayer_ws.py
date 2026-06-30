"""The ``/ws/multiplayer`` WebSocket endpoint for two-human real-time games.

This module is a **thin transport adapter** over :mod:`chess_ai.rooms.manager`.
Its only responsibilities are to (a) parse inbound JSON into protocol messages,
(b) call the matching :class:`~chess_ai.rooms.manager.RoomManager` method,
(c) send direct replies to *this* socket, and (d) broadcast shared game updates
to *both* players. Every piece of authoritative logic -- room codes, player
slots, ``board.is_legal`` validation, turn order, the 60-second disconnect
forfeit timer, reconnect replay, and resignation -- lives in the manager. There
is no ``chess.Board`` and no legality check in this file by design.

Authority and validation (Constraint 12):
    The manager owns one authoritative ``chess.Board`` per room.
    :meth:`RoomManager.apply_move` validates every inbound move with
    ``board.is_legal`` and a turn check and returns a
    :class:`~chess_ai.rooms.manager.MoveResult`. When the result is not ``ok``
    this handler forwards the manager's :class:`~chess_ai.rooms.protocol.ErrorMessage`
    to the offending socket only and does NOT relay or advance the position, so
    an illegal or out-of-turn move can never reach the opponent.

Transport only (Constraint 16):
    Multiplayer moves travel exclusively over this socket. REST is reserved for
    health and initial load elsewhere; no move handling happens over HTTP.

Single shared registry:
    A single module-level :data:`manager` instance backs ``/ws/multiplayer`` so
    that both players' connections observe the same rooms. ``chess_ai.app``
    mounts this module's :data:`router` via ``app.include_router(...)``, which
    publishes the ``/ws/multiplayer`` path.

Direct reply vs. broadcast:
    Direct replies (``room_created``, ``room_joined``, ``error``, and the
    reconnect catch-up ``state``) go to THIS socket with
    ``websocket.send_text(protocol.serialize(message))``. Shared updates
    (``state`` after an applied move and ``game_over``) go to BOTH players via
    :meth:`RoomManager.broadcast`, which awaits each slot's registered ``send``
    callable with ``message.to_dict()`` -- hence the per-connection ``send``
    defined here accepts a ``dict``.

Correlation id:
    WebSocket connections are not covered by the HTTP correlation-id
    middleware, so this handler binds a fresh per-connection id explicitly for
    structured logging.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from chess_ai.observability import metrics
from chess_ai.observability.logging_config import (
    bind_correlation_id,
    bind_log_context,
    clear_log_context,
    get_logger,
)
from chess_ai.observability.tracing import get_tracer
from chess_ai.rooms import protocol
from chess_ai.rooms.manager import RoomManager

__all__ = ["router", "manager", "multiplayer_endpoint"]

# Module logger (structured, event-style: ``logger.info("event", key=value)``).
logger = get_logger(__name__)

# The router app.py mounts via ``app.include_router(multiplayer_ws.router)``.
# The websocket route below declares its absolute path, mirroring the no-prefix
# convention used by the health router.
router = APIRouter(tags=["multiplayer"])

# THE single shared room registry for all multiplayer connections. Both players
# of a game connect to the same process and resolve their room through this one
# instance, so it must be module-level (not per-connection). The default
# 60-second disconnect timeout and 6-character code length are used.
manager = RoomManager()

# Endpoint label for the multiplayer Prometheus metrics (matches the fixed
# enumeration used by ``chess_ai.observability.metrics``).
_ENDPOINT = "multiplayer"

# Wire value of the illegal-move error code, compared against the manager's
# ErrorMessage to drive the rejection metric. Using the literal avoids importing
# the ErrorCode enum; an ErrorCode member compares equal to this string.
_ILLEGAL_MOVE_CODE = "illegal_move"


@router.websocket("/ws/multiplayer")
async def multiplayer_endpoint(websocket: WebSocket) -> None:
    """Serve one multiplayer WebSocket connection end to end.

    Accepts the socket, binds per-connection logging context, then runs a
    receive loop that dispatches each inbound message to the room manager and
    performs the socket I/O. The connection's identity within a room
    (``code`` / ``player_token`` / ``color``) is learned after a successful
    ``create_room``, ``join_room``, or ``reconnect`` and kept as coroutine-local
    state; the durable room/slot state lives in the manager, keyed by code and
    token.

    Args:
        websocket: The inbound Starlette/FastAPI WebSocket connection.
    """
    await websocket.accept()

    # WebSocket connections are not covered by CorrelationIdMiddleware, so set a
    # fresh correlation id and connection-scoped log context explicitly.
    cid = uuid.uuid4().hex
    bind_correlation_id(cid)
    bind_log_context(mode=_ENDPOINT, conn_id=cid)

    # This socket's identity within a room, populated on create/join/reconnect.
    code: str | None = None
    player_token: str | None = None
    color: str | None = None

    async def send(payload: dict) -> None:
        """Per-connection push used by ``manager.broadcast`` (accepts a dict).

        The manager calls ``slot.send(message.to_dict())`` to fan a message out
        to both players, so this callable takes the already-serialized dict and
        writes it to this socket as a JSON text frame.

        Args:
            payload: The message as a plain ``dict`` with snake_case keys.
        """
        await websocket.send_json(payload)

    try:
        # Count this open WebSocket connection for the whole session; the gauge
        # decrements on exit (including when the body raises) so it never leaks.
        # Active multiplayer GAMES are tracked separately by the room manager at
        # each room's activation and finish boundaries -- a lobby-only socket
        # holds no game, and a two-player game is one game, not two -- so this
        # handler deliberately does not also count active games per socket.
        with metrics.track_ws_connection(_ENDPOINT):
            while True:
                raw = await websocket.receive_text()

                # Parse inbound JSON into a protocol message. A malformed payload
                # is rejected with an ``invalid_message`` error and the loop
                # continues so one bad frame never tears down the connection.
                try:
                    message = protocol.parse_client_message(raw)
                except protocol.ProtocolError as exc:
                    await websocket.send_text(
                        protocol.serialize(
                            protocol.ErrorMessage(
                                code="invalid_message",
                                message=str(exc),
                            )
                        )
                    )
                    continue

                # ---------------------------------------------------------
                # create_room: seat the creator as white. Reply to THIS socket
                # only -- the second player is not present yet, so there is
                # nothing to broadcast.
                # ---------------------------------------------------------
                if isinstance(message, protocol.CreateRoomMessage):
                    created = manager.create_room()
                    code = created.code
                    player_token = created.player_token
                    color = created.color
                    manager.register_connection(code, color, send)
                    bind_log_context(room=code, color=color)
                    await websocket.send_text(protocol.serialize(created))
                    logger.info("room_created", room=code, color=color)

                # ---------------------------------------------------------
                # join_room: seat the joiner as black and activate the game. On
                # success, reply ``room_joined`` to THIS socket, then broadcast
                # the now-active state to BOTH players.
                # ---------------------------------------------------------
                elif isinstance(message, protocol.JoinRoomMessage):
                    result = manager.join_room(message.code)
                    if isinstance(result, protocol.ErrorMessage):
                        await websocket.send_text(protocol.serialize(result))
                        logger.info("join_rejected", code=message.code, error=result.code)
                        continue
                    code = result.code
                    player_token = result.player_token
                    color = result.color
                    manager.register_connection(code, color, send)
                    bind_log_context(room=code, color=color)
                    await websocket.send_text(protocol.serialize(result))
                    room = manager.get_room(code)
                    if room is not None:
                        await manager.broadcast(room, manager.build_state(room))
                    logger.info("room_joined", room=code, color=color)

                # ---------------------------------------------------------
                # move: validate and apply through the manager (Constraint 12).
                # A rejected move's error is forwarded to the offending socket
                # only; an accepted move's state (and any game_over) is
                # broadcast to BOTH players.
                # ---------------------------------------------------------
                elif isinstance(message, protocol.MoveMessage):
                    if code is None or player_token is None:
                        await websocket.send_text(
                            protocol.serialize(
                                protocol.ErrorMessage(
                                    code="invalid_message",
                                    message="Create or join a room before moving.",
                                )
                            )
                        )
                        continue

                    # Trace the WebSocket-to-room-manager move path.
                    with get_tracer(__name__).start_as_current_span(
                        "multiplayer.move",
                        attributes={"chess.room": code},
                    ):
                        result = manager.apply_move(
                            code,
                            player_token,
                            message.from_square,
                            message.to_square,
                            message.promotion,
                        )

                        if not result.ok:
                            # Server-authoritative rejection: forward the
                            # manager's error to this socket ONLY and do not
                            # relay or advance the position (Constraint 12). A
                            # rejected move is NOT a processed move -- it is
                            # counted only as an illegal rejection here, never in
                            # MOVES_PROCESSED.
                            if result.error is not None:
                                await websocket.send_text(protocol.serialize(result.error))
                                if result.error.code == _ILLEGAL_MOVE_CODE:
                                    metrics.inc_illegal_move(_ENDPOINT)
                                logger.info("move_rejected", room=code, error=result.error.code)
                            continue

                        # Accepted and applied: count exactly one processed move,
                        # then broadcast the new authoritative state to both
                        # players, and the terminal message if the move ended the
                        # game.
                        metrics.inc_move(_ENDPOINT)
                        room = manager.get_room(code)
                        if room is not None and result.state is not None:
                            await manager.broadcast(room, result.state)
                        if result.game_over is not None:
                            if room is not None:
                                await manager.broadcast(room, result.game_over)
                            metrics.record_game_result(result.game_over.result, _ENDPOINT)
                            logger.info("game_over", room=code, result=result.game_over.result)

                # ---------------------------------------------------------
                # reconnect: restore a player into a live room and replay the
                # position. The manager returns the catch-up state; the color is
                # derived from the room's slot for this token.
                # ---------------------------------------------------------
                elif isinstance(message, protocol.ReconnectMessage):
                    result = manager.reconnect(message.code, message.player_token)
                    if isinstance(result, protocol.ErrorMessage):
                        await websocket.send_text(protocol.serialize(result))
                        logger.info("reconnect_rejected", code=message.code, error=result.code)
                        continue
                    # Adopt this connection's identity from the restored room.
                    room = manager.get_room(message.code)
                    if room is not None:
                        code = room.code
                        player_token = message.player_token
                        slot = room.slot_for_token(player_token)
                        color = slot.color if slot is not None else None
                        if color is not None:
                            manager.register_connection(code, color, send)
                            bind_log_context(room=code, color=color)
                    # Catch-up snapshot (FEN + full SAN history) to THIS socket.
                    await websocket.send_text(protocol.serialize(result))
                    logger.info("player_reconnected", room=code, color=color)

                # ---------------------------------------------------------
                # resign: end the game for this player and notify BOTH, then
                # close this connection. Resignation is token-authenticated in
                # the manager.
                # ---------------------------------------------------------
                elif isinstance(message, protocol.ResignMessage):
                    if code is None or player_token is None:
                        await websocket.send_text(
                            protocol.serialize(
                                protocol.ErrorMessage(
                                    code="invalid_message",
                                    message="Create or join a room before resigning.",
                                )
                            )
                        )
                        continue
                    outcome = manager.resign(code, player_token)
                    if isinstance(outcome, protocol.ErrorMessage):
                        await websocket.send_text(protocol.serialize(outcome))
                        logger.info("resign_rejected", room=code, error=outcome.code)
                        continue
                    room = manager.get_room(code)
                    if room is not None:
                        await manager.broadcast(room, outcome)
                    metrics.record_game_result(outcome.result, _ENDPOINT)
                    logger.info("player_resigned", room=code, color=color)
                    break

                # ---------------------------------------------------------
                # Any other parsed message is not valid on this endpoint.
                # (parse_client_message only yields the five handled types, so
                # this is a defensive fallback.)
                # ---------------------------------------------------------
                else:
                    await websocket.send_text(
                        protocol.serialize(
                            protocol.ErrorMessage(
                                code="invalid_message",
                                message="Unsupported message type for this endpoint.",
                            )
                        )
                    )

    except WebSocketDisconnect:
        # Normal client drop. Detach this socket and start the manager's
        # disconnect forfeit timer; the manager broadcasts a timeout game_over
        # to the remaining player if the window elapses without a reconnect.
        logger.info("websocket_disconnected", room=code, color=color)
        if code is not None and color is not None:
            manager.unregister_connection(code, color)
        if code is not None and player_token is not None:
            manager.mark_disconnected(code, player_token)

    except asyncio.CancelledError:
        # Task cancellation (for example, server shutdown). Treat it like a
        # disconnect for cleanup, then propagate so the runtime can finish
        # tearing the task down.
        logger.info("websocket_cancelled", room=code, color=color)
        if code is not None and color is not None:
            manager.unregister_connection(code, color)
        if code is not None and player_token is not None:
            manager.mark_disconnected(code, player_token)
        raise

    except Exception:
        # Unexpected error: log with the full traceback and best-effort detach
        # this socket so a stale send callable is not left registered. The
        # exception is swallowed so it does not leak out of the handler.
        logger.exception("multiplayer_handler_error", room=code, color=color)
        if code is not None and color is not None:
            manager.unregister_connection(code, color)

    finally:
        # Always clear connection-scoped context so it never leaks to the next
        # task that reuses this execution context.
        clear_log_context()
