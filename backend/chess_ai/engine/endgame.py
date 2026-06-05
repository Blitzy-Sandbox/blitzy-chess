"""Syzygy endgame-tablebase probing for the chess engine (pure computation).

This module probes `Syzygy <https://syzygy-tables.info/>`_ endgame tablebases to
return perfect-play information -- win/draw/loss (WDL) and distance-to-zero (DTZ)
-- for positions with a small number of pieces. The search short-circuits to a
tablebase move when six or fewer pieces remain, replacing the heuristic
alpha-beta result with the tablebase's exact answer.

Public API
----------
* :func:`open_tablebase` -- open the Syzygy table directory and return an
  :class:`EndgameTablebase`, or ``None`` when the tables are absent or unreadable.
* :class:`EndgameTablebase` -- a defensive wrapper around python-chess's
  :class:`chess.syzygy.Tablebase` exposing
  :meth:`~EndgameTablebase.should_probe`, :meth:`~EndgameTablebase.probe_wdl`,
  :meth:`~EndgameTablebase.probe_dtz`, :meth:`~EndgameTablebase.probe_best_move`,
  :meth:`~EndgameTablebase.score_cp`, :meth:`~EndgameTablebase.close`, and the
  context-manager protocol.
* :data:`MAX_TABLEBASE_PIECES` -- the inclusive piece-count ceiling (6) at or
  below which probing is attempted.

WDL / DTZ semantics
-------------------
python-chess reports both metrics from the *side-to-move* perspective:

* WDL (win/draw/loss): ``+2`` win, ``+1`` cursed win (won, but drawn under the
  fifty-move rule), ``0`` draw, ``-1`` blessed loss, ``-2`` loss.
* DTZ (distance to zero): signed plies to the next zeroing move (a capture or a
  pawn move); positive when the side to move wins, negative when it loses.

When :meth:`~EndgameTablebase.probe_best_move` compares child positions reached
via :meth:`chess.Board.push`, the side to move has flipped, so the child metrics
are negated to express them from the parent mover's perspective.

Purity
------
This is a pure-computation engine module. It imports only ``chess`` /
``chess.syzygy``, the Python standard library (``logging``, ``os``,
``pathlib``), and :mod:`chess_ai.config`. It imports no FastAPI, Starlette,
WebSocket, or asyncio code and exposes only plain ``def`` callables, so it is
safe to call from inside a worker thread and to unit-test in isolation.

Graceful degradation
--------------------
The Syzygy tables are an optional downloaded artifact (fetched by
``make download-syzygy``). When the directory is missing, empty, or corrupt,
:func:`open_tablebase` returns ``None`` and the search proceeds without endgame
knowledge. Every probe entry point likewise returns ``None`` for a position the
installed tables do not cover, rather than raising, so a partial or absent table
set can never crash the application or corrupt a search.

Concurrency
-----------
A single :class:`chess.syzygy.Tablebase` handle is not safe for concurrent
probes. The search uses one handle from a single worker thread per move, which
is safe; callers must not share one handle across concurrent searches without
external synchronization.
"""

import logging
import os
from pathlib import Path

import chess
import chess.syzygy

from chess_ai.config import TABLES_DIR

logger = logging.getLogger(__name__)

# Inclusive piece-count ceiling at or below which the Syzygy tables are probed.
# Standard Syzygy distributions cover up to seven pieces; this project targets
# six-or-fewer per the engine specification.
MAX_TABLEBASE_PIECES: int = 6

# Coarse centipawn magnitudes used by :meth:`EndgameTablebase.score_cp` to map a
# WDL result onto a score the search can consume. A full win/loss is decisive; a
# cursed win / blessed loss is technically winning/losing but neutralized by the
# fifty-move rule, so it maps near the draw value.
TABLEBASE_WIN_CP: int = 20000
TABLEBASE_CURSED_WIN_CP: int = 50

# Exceptions that signal a position the installed tables cannot answer.
# ``MissingTableError`` is itself a ``KeyError`` subclass; ``KeyError``,
# ``ValueError`` and ``IndexError`` cover the remaining decode/lookup failures
# python-chess may raise for an uncovered or malformed entry.
_PROBE_MISS_ERRORS: tuple[type[Exception], ...] = (
    chess.syzygy.MissingTableError,
    KeyError,
    ValueError,
    IndexError,
)

# Filename globs identifying Syzygy WDL (``.rtbw``) and DTZ (``.rtbz``) tables.
_TABLE_GLOBS: tuple[str, ...] = ("*.rtbw", "*.rtbz")


class EndgameTablebase:
    """Defensive wrapper around a python-chess Syzygy tablebase handle.

    Instances are created by :func:`open_tablebase`; construct one directly only
    when supplying a custom handle (for example in tests). The wrapped handle is
    kept open for the lifetime of the instance -- it is opened once during
    application startup and closed on shutdown -- because re-opening the table
    files on every move would be needlessly expensive.

    Every public method degrades gracefully: a position the installed tables do
    not cover yields ``None`` rather than raising, and :meth:`probe_best_move`
    keeps the supplied board's :meth:`~chess.Board.push` / :meth:`~chess.Board.pop`
    stack strictly balanced so a probe failure can never corrupt the position the
    search passed in.
    """

    def __init__(
        self,
        tablebase: chess.syzygy.Tablebase,
        directory: str | os.PathLike[str],
    ) -> None:
        """Wrap an open Syzygy tablebase handle.

        Args:
            tablebase: An open :class:`chess.syzygy.Tablebase` (or any object
                exposing the same ``probe_wdl`` / ``probe_dtz`` / ``close``
                interface).
            directory: The directory the handle was opened from, retained for
                logging and :func:`repr`.
        """
        self._tb = tablebase
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """The directory the Syzygy tables were loaded from."""
        return self._directory

    def should_probe(self, board: chess.Board) -> bool:
        """Report whether ``board`` is small enough to probe.

        Args:
            board: The position to test.

        Returns:
            ``True`` when the total piece count is at most
            :data:`MAX_TABLEBASE_PIECES`, ``False`` otherwise.
        """
        return chess.popcount(board.occupied) <= MAX_TABLEBASE_PIECES

    def probe_wdl(self, board: chess.Board) -> int | None:
        """Probe the win/draw/loss value of ``board`` from the side-to-move POV.

        Args:
            board: The position to probe.

        Returns:
            The WDL value (``-2``..``+2``) when the position is covered, or
            ``None`` when it is too large to probe or the installed tables do
            not cover it.
        """
        if not self.should_probe(board):
            return None
        try:
            return self._tb.probe_wdl(board)
        except _PROBE_MISS_ERRORS:
            return None

    def probe_dtz(self, board: chess.Board) -> int | None:
        """Probe the distance-to-zero of ``board`` from the side-to-move POV.

        Args:
            board: The position to probe.

        Returns:
            The signed DTZ value when the position is covered, or ``None`` when
            it is too large to probe or the installed tables do not cover it.
        """
        if not self.should_probe(board):
            return None
        try:
            return self._tb.probe_dtz(board)
        except _PROBE_MISS_ERRORS:
            return None

    def probe_best_move(self, board: chess.Board) -> chess.Move | None:
        """Select the optimal move for ``board`` using tablebase results.

        Each legal move is played, the resulting child position is probed, and
        the child's metrics are negated to express them from the moving side's
        perspective. The chosen move maximizes the tuple ``(wdl, -dtz)``: it
        first maximizes WDL (a win beats a draw beats a loss); among winning
        moves it minimizes DTZ to mate as fast as possible; among losing moves
        it maximizes the distance to resist as long as possible; among drawing
        moves any drawing move qualifies.

        The board's move stack is restored after every probe via ``try``/
        ``finally``, so the position is unchanged on return regardless of which
        children the tables can answer.

        Args:
            board: The position to choose a move for.

        Returns:
            The best :class:`chess.Move` when at least one child position is
            covered, or ``None`` when the position is too large or no child can
            be probed (so the caller falls through to a normal search).
        """
        if not self.should_probe(board):
            return None

        best_move: chess.Move | None = None
        best_key: tuple[int, int] | None = None

        for move in list(board.legal_moves):
            board.push(move)
            try:
                child_wdl = self.probe_wdl(board)
                child_dtz = self.probe_dtz(board)
            finally:
                board.pop()

            if child_wdl is None:
                continue

            parent_wdl = -child_wdl
            parent_dtz = -(child_dtz if child_dtz is not None else 0)
            key = (parent_wdl, -parent_dtz)
            if best_key is None or key > best_key:
                best_key = key
                best_move = move

        return best_move

    def score_cp(self, board: chess.Board) -> int | None:
        """Map ``board``'s WDL result onto a coarse centipawn score.

        The score is expressed from the side-to-move perspective, matching the
        negamax convention the search uses, so the search can populate a
        tablebase move's score directly.

        Args:
            board: The position to score.

        Returns:
            A centipawn score (``+/-`` :data:`TABLEBASE_WIN_CP` for a decisive
            result, ``+/-`` :data:`TABLEBASE_CURSED_WIN_CP` for a cursed win /
            blessed loss, ``0`` for a draw), or ``None`` when the position is
            not covered.
        """
        wdl = self.probe_wdl(board)
        if wdl is None:
            return None
        if wdl >= 2:
            return TABLEBASE_WIN_CP
        if wdl == 1:
            return TABLEBASE_CURSED_WIN_CP
        if wdl == 0:
            return 0
        if wdl == -1:
            return -TABLEBASE_CURSED_WIN_CP
        return -TABLEBASE_WIN_CP

    def close(self) -> None:
        """Close the underlying tablebase handle, releasing its file handles.

        Safe to call more than once; any error while closing is logged at debug
        level and swallowed so shutdown is never interrupted.
        """
        try:
            self._tb.close()
        except Exception as exc:
            logger.debug("Error while closing Syzygy tablebase: %s", exc)

    def __enter__(self) -> "EndgameTablebase":
        """Enter the runtime context and return this instance."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit the runtime context, closing the tablebase handle."""
        self.close()

    def __repr__(self) -> str:
        return f"EndgameTablebase(directory={str(self._directory)!r})"


def _has_table_files(directory: Path) -> bool:
    """Report whether ``directory`` contains at least one Syzygy table file.

    Args:
        directory: An existing directory to scan.

    Returns:
        ``True`` if at least one ``*.rtbw`` or ``*.rtbz`` file is present,
        ``False`` otherwise.
    """
    for pattern in _TABLE_GLOBS:
        if next(directory.glob(pattern), None) is not None:
            return True
    return False


def open_tablebase(
    directory: str | os.PathLike[str] | None = None,
) -> EndgameTablebase | None:
    """Open the Syzygy tablebase directory and wrap it in an :class:`EndgameTablebase`.

    Args:
        directory: Directory holding the Syzygy ``*.rtbw`` / ``*.rtbz`` files.
            Defaults to :data:`chess_ai.config.TABLES_DIR` when ``None``.

    Returns:
        An :class:`EndgameTablebase` when the directory exists, contains at least
        one table file, and opens cleanly; otherwise ``None``. A missing, empty,
        or corrupt directory is logged and degraded to ``None`` rather than
        raised, so application startup is never blocked by absent optional tables.
    """
    tables_dir = Path(directory) if directory is not None else Path(TABLES_DIR)

    try:
        if not tables_dir.exists():
            logger.warning(
                "Syzygy tables directory %s does not exist; the AI will play "
                "without endgame tablebases.",
                tables_dir,
            )
            return None
        if not tables_dir.is_dir():
            logger.warning(
                "Syzygy tables path %s is not a directory; the AI will play "
                "without endgame tablebases.",
                tables_dir,
            )
            return None
        if not _has_table_files(tables_dir):
            logger.info(
                "No Syzygy table files (*.rtbw/*.rtbz) found in %s; the AI will "
                "play without endgame tablebases.",
                tables_dir,
            )
            return None
        handle = chess.syzygy.open_tablebase(str(tables_dir))
    except Exception as exc:
        logger.warning(
            "Failed to open Syzygy tablebase at %s (%s); continuing without endgame tablebases.",
            tables_dir,
            exc,
        )
        return None

    logger.info("Opened Syzygy endgame tablebase from %s.", tables_dir)
    return EndgameTablebase(handle, tables_dir)


__all__ = ["EndgameTablebase", "MAX_TABLEBASE_PIECES", "open_tablebase"]
