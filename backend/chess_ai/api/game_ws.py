"""The ``/ws/game`` WebSocket endpoint for single-player games against the AI.

This module is the **transport edge** that drives the pure, hand-built chess
engine for a human-versus-AI game. It owns one authoritative ``chess.Board``
per connection and is the only place that applies moves to it, so the server
-- not the client's display board -- is the single source of truth
(Constraint 1). ``chess_ai.app`` mounts this module's :data:`router` via
``app.include_router(game_ws.router)``, which publishes the ``/ws/game`` path.

Responsibilities:
    * Accept a connection, read its ``difficulty`` and ``color`` query params,
      and resolve the difficulty tier into a :class:`~chess_ai.engine.search.SearchLimits`.
    * Validate every inbound human move with ``board.is_legal`` BEFORE applying
      it and reject illegal moves with an ``illegal_move`` error, never
      advancing the position (Constraint 12).
    * Reply to each accepted move with the new authoritative state, then have
      the AI reply.
    * Run the SYNCHRONOUS engine search off the event loop with
      ``asyncio.to_thread`` so the loop is never blocked (Constraint 2), while
      streaming per-depth ``ai_thinking`` progress to the client.
    * Pace every AI move so it is never sent faster than ``MIN_AI_DELAY_MS``
      (1500 ms), whether the move came from the opening book, a searched node,
      or an endgame tablebase (Constraint 8).

Transport only (Constraint 16):
    Game moves travel exclusively over this socket. REST is reserved for health
    and initial load elsewhere; no move handling happens over HTTP.

Engine boundary and purity:
    This file imports the pure :mod:`chess_ai.engine` package (the dependency
    direction api -> engine is allowed) but the engine never imports the web
    layer. The synchronous :meth:`~chess_ai.engine.search.Searcher.search` is
    offloaded with ``asyncio.to_thread``; the engine functions stay synchronous.

Thread-safety:
    python-chess ``Board`` objects are not thread-safe and the searcher pushes
    and pops moves on the board it is given, so the worker thread receives a
    ``board.copy()``. The event loop keeps the untouched original for rendering
    the principal variation and for applying the chosen move afterward.

Correlation id:
    WebSocket connections are not covered by the HTTP ``CorrelationIdMiddleware``,
    so this handler binds a fresh per-connection correlation id and structured
    logging context explicitly.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import chess
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from chess_ai.config import MIN_AI_DELAY_MS, get_tier
from chess_ai.engine.search import Searcher, SearchInfo, SearchLimits, SearchResult
from chess_ai.observability import metrics
from chess_ai.observability.logging_config import (
    bind_correlation_id,
    bind_log_context,
    clear_log_context,
    get_logger,
)
from chess_ai.observability.tracing import get_tracer
from chess_ai.rooms import protocol
from chess_ai.rooms.protocol import (
    AiThinkingMessage,
    ErrorMessage,
    GameOverMessage,
    MoveMessage,
    ResignMessage,
    StateMessage,
)

__all__ = ["router", "game_endpoint"]

# Module logger (structured, event-style: ``logger.info("event", key=value)``).
logger = get_logger(__name__)

# The router app.py mounts via ``app.include_router(game_ws.router)``. The
# websocket route below declares its absolute path, mirroring the no-prefix
# convention used by the health and multiplayer routers.
router = APIRouter(tags=["game"])

# Prometheus label for this endpoint's connection/illegal-move metrics and the
# game-mode label for move/result/active-game metrics. Both come from the fixed
# enumerations used by ``chess_ai.observability.metrics``.
_ENDPOINT = "game"
_MODE = "ai"

# Sentinel pushed onto the AI-thinking queue to tell the drain coroutine that
# the search is finished and it should stop. A unique object so it can never be
# confused with a real ``SearchInfo`` payload.
_SENTINEL = object()


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def _square_name(square: int) -> str:
    """Return the algebraic name (e.g. ``"e4"``) for a python-chess square int."""
    return chess.square_name(square)


def _opponent(color: str) -> str:
    """Return the opposing color string for ``"white"`` / ``"black"``."""
    return "black" if color == "white" else "white"


def _san_pv(base: chess.Board, pv: list[chess.Move]) -> list[str]:
    """Render a principal variation as SAN strings against a copy of ``base``.

    The principal variation arrives from the engine as raw moves. SAN depends on
    the position each move is made from, so the moves are replayed on a private
    copy of ``base``; the caller's board is never mutated. Rendering stops at the
    first move that is not legal in the running line, which keeps a partial or
    stale variation from raising.

    Args:
        base: The position the variation starts from (left untouched).
        pv: The principal variation as a list of moves.

    Returns:
        The variation as a list of SAN strings, possibly truncated.
    """
    rendered: list[str] = []
    board = base.copy()
    for move in pv:
        if not board.is_legal(move):
            break
        rendered.append(board.san(move))
        board.push(move)
    return rendered


# ---------------------------------------------------------------------------
# State and terminal-detection helpers
# ---------------------------------------------------------------------------
def _build_state(board: chess.Board, move_history: list[str]) -> StateMessage:
    """Build a :class:`StateMessage` snapshot of the authoritative position.

    Args:
        board: The authoritative board.
        move_history: SAN strings for every move played so far, in order.

    Returns:
        A :class:`StateMessage` carrying the FEN, the SAN history, whose turn it
        is, whether the game is finished, the check flag, and the last move's
        ``from_square`` / ``to_square`` (or ``None`` before the first move).
    """
    last_move: dict[str, str] | None = None
    if board.move_stack:
        recent = board.peek()
        last_move = {
            "from_square": _square_name(recent.from_square),
            "to_square": _square_name(recent.to_square),
        }
    return StateMessage(
        fen=board.fen(),
        move_history=list(move_history),
        turn="white" if board.turn == chess.WHITE else "black",
        status="finished" if board.is_game_over() else "active",
        in_check=board.is_check(),
        last_move=last_move,
    )


def _check_game_over(board: chess.Board) -> GameOverMessage | None:
    """Return a :class:`GameOverMessage` if the position is terminal, else ``None``.

    The checks cover exactly the conditions python-chess reports through
    ``board.is_game_over()`` with ``claim_draw=False``: checkmate, stalemate,
    insufficient material, the seventy-five-move rule, and fivefold repetition.

    Args:
        board: The authoritative board to inspect.

    Returns:
        A terminal :class:`GameOverMessage`, or ``None`` for a live position.
    """
    if board.is_checkmate():
        # After the mating move ``board.turn`` is the side that was checkmated,
        # so the winner is the other color.
        winner = "white" if board.turn == chess.BLACK else "black"
        return GameOverMessage(
            result="checkmate",
            winner=winner,
            reason=f"{winner.capitalize()} wins by checkmate",
        )
    if board.is_stalemate():
        return GameOverMessage(result="stalemate", winner=None, reason="Draw by stalemate")
    if board.is_insufficient_material():
        return GameOverMessage(result="draw", winner=None, reason="Draw by insufficient material")
    if board.is_seventyfive_moves():
        return GameOverMessage(
            result="draw", winner=None, reason="Draw by the seventy-five-move rule"
        )
    if board.is_fivefold_repetition():
        return GameOverMessage(result="draw", winner=None, reason="Draw by fivefold repetition")
    return None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
@router.websocket("/ws/game")
async def game_endpoint(websocket: WebSocket) -> None:
    """Serve one single-player human-versus-AI WebSocket connection end to end.

    The connection is parameterized by two query params:

    * ``difficulty`` -- one of ``"easy"``, ``"medium"``, ``"hard"``; defaults to
      ``"medium"`` when missing or unrecognized.
    * ``color`` -- the human's color, ``"white"`` or ``"black"``; defaults to
      ``"white"``. When the human is Black the AI is White and moves first.

    "New game" is expressed by the client opening a fresh connection (the
    frontend hook reconnects), so the only inbound messages handled here are
    ``move`` and ``resign``; anything else is answered with an
    ``invalid_message`` error.

    Args:
        websocket: The inbound Starlette/FastAPI WebSocket connection.
    """
    await websocket.accept()

    # --- Resolve handshake params (defensively; never trust the client). -----
    difficulty = (websocket.query_params.get("difficulty") or "medium").lower()
    human_color = (websocket.query_params.get("color") or "white").lower()
    if human_color not in ("white", "black"):
        human_color = "white"

    try:
        tier = get_tier(difficulty)
    except Exception:
        # Unknown tier string: fall back to medium and normalize the label so the
        # logs, metrics, and the tier all agree.
        logger.warning("unknown_difficulty", difficulty=difficulty)
        difficulty = "medium"
        tier = get_tier("medium")
    limits = SearchLimits.from_tier(tier)

    # --- Per-connection correlation id and structured logging context. -------
    cid = uuid.uuid4().hex
    bind_correlation_id(cid)
    bind_log_context(mode=_MODE, difficulty=difficulty, game_id=cid)

    # --- Authoritative game state (Constraint 1). ----------------------------
    board = chess.Board()
    move_history: list[str] = []

    # Shared engine resources loaded by the app lifespan; read defensively so a
    # missing book or tablebase simply disables that feature.
    book = getattr(websocket.app.state, "opening_book", None)
    tablebase = getattr(websocket.app.state, "tablebase", None)

    # One searcher per connection: its transposition table, killers, and history
    # persist across the moves of this game and improve ordering as it proceeds.
    searcher = Searcher(book=book, tablebase=tablebase)

    logger.info(
        "game_started",
        difficulty=difficulty,
        human_color=human_color,
        depth=limits.depth,
    )

    try:
        # Count the open connection and the active game for the whole session.
        # Both context managers decrement on exit -- including when the body
        # raises -- so the gauges never leak.
        with (
            metrics.track_ws_connection(_ENDPOINT),
            metrics.track_active_game(_MODE),
        ):
            # Send the opening position so the client can render immediately.
            await websocket.send_text(protocol.serialize(_build_state(board, move_history)))

            # If the human is Black, the AI is White and opens the game.
            if human_color == "black":
                await _play_ai_move(websocket, board, move_history, searcher, limits, difficulty)
                await _maybe_finish(websocket, board)

            # Main receive loop: handle one inbound frame per iteration.
            while True:
                raw = await websocket.receive_text()

                # Parse inbound JSON into a protocol message. A malformed frame
                # is rejected with ``invalid_message`` and the loop continues so
                # one bad frame never tears down the connection.
                try:
                    message = protocol.parse_client_message(raw)
                except protocol.ProtocolError as exc:
                    await websocket.send_text(
                        protocol.serialize(ErrorMessage(code="invalid_message", message=str(exc)))
                    )
                    continue

                # ---------------------------------------------------------
                # move: validate server-side, apply, then let the AI reply.
                # ---------------------------------------------------------
                if isinstance(message, MoveMessage):
                    handled = await _handle_human_move(websocket, board, move_history, message)
                    if not handled:
                        # Illegal or unparseable: the rejection was already sent
                        # and the position is unchanged (Constraint 12).
                        continue

                    # The human's move may itself end the game; if so, do not
                    # let the AI move.
                    if await _maybe_finish(websocket, board):
                        continue

                    # The AI replies, then we re-check for a terminal position
                    # the AI's move may have created.
                    await _play_ai_move(
                        websocket, board, move_history, searcher, limits, difficulty
                    )
                    await _maybe_finish(websocket, board)

                # ---------------------------------------------------------
                # resign: the human concedes; the AI's color wins.
                # ---------------------------------------------------------
                elif isinstance(message, ResignMessage):
                    winner = _opponent(human_color)
                    await websocket.send_text(
                        protocol.serialize(
                            GameOverMessage(
                                result="resignation",
                                winner=winner,
                                reason=f"{human_color.capitalize()} resigned",
                            )
                        )
                    )
                    metrics.record_game_result("resignation", _MODE)
                    logger.info("player_resigned", winner=winner)
                    break

                # ---------------------------------------------------------
                # Any other parsed type is not valid on this endpoint.
                # ---------------------------------------------------------
                else:
                    await websocket.send_text(
                        protocol.serialize(
                            ErrorMessage(
                                code="invalid_message",
                                message="Unsupported message type for /ws/game.",
                            )
                        )
                    )

    except WebSocketDisconnect:
        # Normal client drop (possibly mid-search). Nothing to clean up beyond
        # the logging context: the board is per-connection coroutine state.
        logger.info("websocket_disconnected", game_id=cid)

    except asyncio.CancelledError:
        # Task cancellation (for example, server shutdown). Log and propagate so
        # the runtime can finish tearing the task down.
        logger.info("websocket_cancelled", game_id=cid)
        raise

    except Exception:
        # Unexpected error: log with the full traceback and swallow so it does
        # not leak out of the handler. The connection closes on return.
        logger.exception("game_handler_error", game_id=cid)

    finally:
        # Always clear connection-scoped context so it never leaks to the next
        # task that reuses this execution context.
        clear_log_context()


async def _handle_human_move(
    websocket: WebSocket,
    board: chess.Board,
    move_history: list[str],
    message: MoveMessage,
) -> bool:
    """Validate and apply one inbound human move to the authoritative board.

    Reconstructs the move from the message's ``from_square`` / ``to_square`` /
    ``promotion``, rejects it with an ``illegal_move`` error if it cannot be
    parsed or is not legal in the current position (Constraint 12), and
    otherwise applies it and broadcasts the new state.

    Args:
        websocket: The connection to reply on.
        board: The authoritative board (mutated only on a legal move).
        move_history: SAN history appended to on a legal move.
        message: The inbound :class:`MoveMessage`.

    Returns:
        ``True`` if the move was legal and applied; ``False`` if it was rejected
        (in which case the position is unchanged and an error has been sent).
    """
    uci = message.from_square + message.to_square + (message.promotion or "")
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        await websocket.send_text(
            protocol.serialize(
                ErrorMessage(code="illegal_move", message=f"Unparseable move: {uci}")
            )
        )
        metrics.inc_illegal_move(_ENDPOINT)
        logger.info("illegal_move_rejected", uci=uci, reason="unparseable")
        return False

    # Server-authoritative legality check BEFORE applying (Constraint 12).
    if not board.is_legal(move):
        await websocket.send_text(
            protocol.serialize(ErrorMessage(code="illegal_move", message="Illegal move"))
        )
        metrics.inc_illegal_move(_ENDPOINT)
        logger.info("illegal_move_rejected", uci=uci, reason="illegal")
        return False

    # SAN must be computed BEFORE the move is pushed (it needs the pre-move
    # position), then the move is applied to the authoritative board.
    san = board.san(move)
    board.push(move)
    move_history.append(san)
    metrics.inc_move(_MODE)
    await websocket.send_text(protocol.serialize(_build_state(board, move_history)))
    return True


async def _maybe_finish(websocket: WebSocket, board: chess.Board) -> bool:
    """Send a ``game_over`` message and record the result if ``board`` is terminal.

    Args:
        websocket: The connection to notify.
        board: The authoritative board to inspect.

    Returns:
        ``True`` if the game is over (a ``game_over`` message was sent), else
        ``False``.
    """
    game_over = _check_game_over(board)
    if game_over is None:
        return False
    await websocket.send_text(protocol.serialize(game_over))
    metrics.record_game_result(game_over.result, _MODE)
    logger.info("game_over", result=game_over.result, winner=game_over.winner)
    return True


# ---------------------------------------------------------------------------
# The AI move: non-blocking search, streamed thinking, and move pacing
# ---------------------------------------------------------------------------
async def _play_ai_move(
    websocket: WebSocket,
    board: chess.Board,
    move_history: list[str],
    searcher: Searcher,
    limits: SearchLimits,
    difficulty: str,
) -> None:
    """Compute and apply the AI's move, streaming progress and pacing the reply.

    The flow, in order:

    1. Return immediately if the position is already terminal.
    2. Start a drain coroutine that turns streamed
       :class:`~chess_ai.engine.search.SearchInfo` into ``ai_thinking`` messages.
    3. Run the synchronous search on a ``board.copy()`` via ``asyncio.to_thread``
       so the event loop is never blocked (Constraint 2), inside a tracing span.
    4. Pace the reply so at least ``MIN_AI_DELAY_MS`` elapses since the AI began
       thinking, regardless of whether the move came from the book, search, or a
       tablebase (Constraint 8).
    5. Apply the chosen move to the authoritative board and broadcast the state.

    Args:
        websocket: The connection to stream thinking and the new state on.
        board: The authoritative board (mutated with the AI's move).
        move_history: SAN history appended to with the AI's move.
        searcher: The per-connection searcher.
        limits: The search depth and time budget for the difficulty tier.
        difficulty: The difficulty label, used for metrics and tracing.
    """
    # Nothing to do if the human's move (or the opening) already ended the game.
    if board.is_game_over():
        return

    t0 = time.monotonic()

    # Marshal streamed progress from the worker thread onto the event loop. The
    # worker calls ``on_info`` (cheaply); the drain coroutine does the formatting
    # and the I/O on the loop so the board copy used for SAN is never shared with
    # the worker thread.
    loop = asyncio.get_running_loop()
    think_queue: asyncio.Queue = asyncio.Queue()
    ai_color = board.turn  # The side to move is the AI; used for the eval sign.

    def on_info(info: SearchInfo) -> None:
        """Runs IN the worker thread: hand the info to the loop's queue."""
        try:
            loop.call_soon_threadsafe(think_queue.put_nowait, info)
        except RuntimeError:
            # The loop is closing/closed; drop the late update silently.
            pass

    async def _drain() -> None:
        """Runs ON the event loop: stream ``ai_thinking`` until the sentinel."""
        while True:
            info = await think_queue.get()
            if info is _SENTINEL:
                break
            try:
                # SearchInfo scores are from the side-to-move (AI) POV; the wire
                # contract is White-POV centipawns, so flip the sign for Black.
                evaluation = info.score_cp if ai_color == chess.WHITE else -info.score_cp
                message = AiThinkingMessage(
                    depth=info.depth,
                    evaluation=int(evaluation),
                    pv=_san_pv(board, info.pv),
                    nodes=info.nodes,
                    time_s=info.time_s,
                )
                await websocket.send_text(protocol.serialize(message))
            except Exception as exc:
                # A slow or closed client must not crash the drain task; stop
                # streaming and let the main path surface any disconnect.
                logger.info("ai_thinking_stream_stopped", error=str(exc))
                break

    drain_task = asyncio.create_task(_drain())

    # Thread-safety (Constraint 2): python-chess Board is not thread-safe and the
    # searcher pushes/pops on the board it receives, so the worker gets a COPY
    # and never touches the board the drain coroutine reads.
    search_board = board.copy()

    try:
        with get_tracer(__name__).start_as_current_span(
            "engine.search",
            attributes={"chess.difficulty": difficulty, "chess.depth": limits.depth},
        ):
            result: SearchResult = await asyncio.to_thread(
                searcher.search, search_board, limits, info_callback=on_info
            )
    finally:
        # Always stop and await the drain task, even if the search raised, so the
        # task can never leak.
        think_queue.put_nowait(_SENTINEL)
        await drain_task

    # Pacing (Constraint 8): never reply faster than MIN_AI_DELAY_MS, including
    # for near-instant book and tablebase moves. Sleep out the remainder.
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if elapsed_ms < MIN_AI_DELAY_MS:
        await asyncio.sleep((MIN_AI_DELAY_MS - elapsed_ms) / 1000.0)

    # Apply the AI move to the AUTHORITATIVE board with a defensive legality
    # check; for a non-terminal position the searcher always returns a legal move.
    move = result.best_move
    if move is None or not board.is_legal(move):
        logger.error(
            "ai_no_legal_move",
            best_move=str(move),
            fen=board.fen(),
        )
        return
    san = board.san(move)
    board.push(move)
    move_history.append(san)

    # Record search and move metrics; the book/tablebase counters are optional
    # helpers, guarded so the endpoint tolerates a trimmed metrics module.
    metrics.record_search(result.time_s, result.nodes, difficulty)
    metrics.inc_move(_MODE)
    if result.from_book:
        inc_book = getattr(metrics, "inc_book_move", None)
        if callable(inc_book):
            inc_book()
    if result.from_tablebase:
        inc_tablebase = getattr(metrics, "inc_tablebase_move", None)
        if callable(inc_tablebase):
            inc_tablebase()

    logger.info(
        "ai_move",
        san=san,
        score_cp=result.score_cp,
        depth=result.depth,
        nodes=result.nodes,
        time_s=round(result.time_s, 3),
        from_book=result.from_book,
        from_tablebase=result.from_tablebase,
    )

    # Broadcast the new authoritative state. The caller checks for a terminal
    # position after this returns.
    await websocket.send_text(protocol.serialize(_build_state(board, move_history)))
