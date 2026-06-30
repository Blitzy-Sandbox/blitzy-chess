"""Observability for the blitzy-chess backend.

Centralizes structured logging (structlog with correlation IDs), OpenTelemetry
tracing, and Prometheus metrics. These concerns are configured at the
application edge by ``chess_ai.app`` and the ``chess_ai.api`` modules, and are
never imported by the pure ``chess_ai.engine`` package.

This package root is intentionally side-effect-free: it imports none of its
submodules (``logging_config``, ``tracing``, ``metrics``); each is imported
explicitly where it is used.
"""
