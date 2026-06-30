"""WebSocket and REST transport endpoints for the chess backend.

Provides the FastAPI routers included by ``chess_ai.app``: ``game_ws``
(``/ws/game``), ``multiplayer_ws`` (``/ws/multiplayer``), and ``health``
(``/health``, ``/health/ready``).

This package root is intentionally side-effect-free: it imports none of its
submodules. ``chess_ai.app`` imports each one explicitly where it is registered.
"""
