"""Multiplayer room management: room lifecycle and the WebSocket message protocol.

Re-exports the public API of the two submodules so consumers can import them
from ``chess_ai.rooms`` directly: room lifecycle and state from ``manager``
(``RoomManager``, ``Room``, ``MoveResult``) and the canonical WebSocket message
contract from ``protocol`` (the message dataclasses, ``ErrorCode``,
``ProtocolError``, and the ``serialize`` / ``parse_client_message`` helpers).

This package root is transport-agnostic: it imports ``chess`` (python-chess)
and the standard library transitively through ``manager``, and never imports
FastAPI, Starlette, uvicorn, or any WebSocket transport code.
"""

from chess_ai.rooms.manager import MoveResult, Room, RoomManager
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
    parse_client_message,
    serialize,
)

__all__ = [
    # Room lifecycle and state (manager).
    "RoomManager",
    "Room",
    "MoveResult",
    # WebSocket message contract (protocol).
    "CreateRoomMessage",
    "JoinRoomMessage",
    "MoveMessage",
    "ReconnectMessage",
    "ResignMessage",
    "RoomCreatedMessage",
    "RoomJoinedMessage",
    "StateMessage",
    "AiThinkingMessage",
    "GameOverMessage",
    "ErrorMessage",
    "ProtocolError",
    "ErrorCode",
    "serialize",
    "parse_client_message",
]
