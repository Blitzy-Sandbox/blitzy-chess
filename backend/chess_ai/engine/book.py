"""Polyglot opening-book probing for the chess engine (pure computation).

This module reads a `Polyglot <http://hgm.nubati.net/book_format.html>`_ opening
book (a sorted binary file of 16-byte entries) and selects an opening move for a
given position using weighted random selection. It is the first thing the
search consults on every move: :class:`OpeningBook.probe` returns a book move
when one exists, and the searcher skips the alpha-beta search for that move.

Public API
----------
* :func:`load_book` -- open the book file and return an :class:`OpeningBook`,
  or ``None`` when the book is absent or unreadable.
* :class:`OpeningBook` -- a thin, defensive wrapper around python-chess's
  :class:`chess.polyglot.MemoryMappedReader` exposing
  :meth:`~OpeningBook.probe`, :meth:`~OpeningBook.list_moves`,
  :meth:`~OpeningBook.has_moves`, :meth:`~OpeningBook.close`, and the
  context-manager protocol.

Purity
------
This is a pure-computation engine module. It imports only ``chess`` /
``chess.polyglot``, the Python standard library (``logging``, ``os``,
``random``, ``pathlib``), and :mod:`chess_ai.config`. It imports no FastAPI,
Starlette, WebSocket, or asyncio code and exposes only plain ``def`` callables,
so it is safe to call from inside a worker thread and to unit-test in isolation.

Move pacing
-----------
This module never sleeps and never blocks on time. Book moves are still subject
to the ``MIN_AI_DELAY_MS = 1500`` minimum move delay, but that pacing is applied
by the WebSocket handler (``api/game_ws.py``), not here. Callers must not assume
probing introduces any delay.

Graceful degradation
--------------------
The Polyglot book is an optional downloaded artifact (fetched by ``make init``).
When the file is missing, empty, or corrupt, :func:`load_book` returns ``None``
and the searcher proceeds directly to search; probing never raises for a missing
book or an out-of-book position.
"""

import logging
import os
import random
from pathlib import Path

import chess
import chess.polyglot

from chess_ai.config import OPENING_BOOK_PATH

logger = logging.getLogger(__name__)


class OpeningBook:
    """Weighted-random opening-move selector backed by a Polyglot reader.

    Instances are created by :func:`load_book`; construct one directly only when
    supplying a custom reader (for example in tests). The wrapped reader is kept
    open for the lifetime of the instance -- it is opened once during application
    startup and closed on shutdown -- because memory-mapping the file on every
    move would be needlessly expensive.

    Every public method degrades gracefully: an out-of-book position yields
    ``None`` / empty results rather than raising, and any returned move is
    validated against ``board.legal_moves`` before it leaves this module so a
    malformed entry can never reach the server-authoritative move pipeline.
    """

    def __init__(
        self, reader: chess.polyglot.MemoryMappedReader, path: str | os.PathLike[str]
    ) -> None:
        """Wrap an open Polyglot reader.

        Args:
            reader: An open :class:`chess.polyglot.MemoryMappedReader` (or any
                object exposing the same ``weighted_choice`` / ``find_all`` /
                ``close`` interface).
            path: The source path the reader was opened from, retained for
                logging and :func:`repr`.
        """
        self._reader = reader
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The filesystem path the opening book was loaded from."""
        return self._path

    def probe(self, board: chess.Board, *, rng: random.Random | None = None) -> chess.Move | None:
        """Select a book move for ``board`` by weighted random choice.

        Args:
            board: The position to look up.
            rng: A seeded :class:`random.Random` for deterministic selection
                (used by tests). Pass ``None`` to use python-chess's module
                default RNG.

        Returns:
            A legal :class:`chess.Move` drawn from the book in proportion to the
            entry weights, or ``None`` when the position is not in the book or
            the chosen move fails legality validation.
        """
        try:
            entry = self._reader.weighted_choice(board, random=rng)
        except IndexError:
            # Expected end-of-book signal: no entries for this position.
            return None
        except Exception as exc:
            logger.warning("Opening-book probe failed for FEN %s: %s", board.fen(), exc)
            return None

        move = entry.move
        if move in board.legal_moves:
            return move

        logger.warning(
            "Opening book returned illegal move %s for FEN %s; ignoring it.",
            move.uci(),
            board.fen(),
        )
        return None

    def list_moves(self, board: chess.Board) -> list[tuple[chess.Move, int]]:
        """List every book move for ``board`` with its weight, heaviest first.

        Args:
            board: The position to look up.

        Returns:
            A list of ``(move, weight)`` pairs sorted by descending weight. The
            searcher uses this to populate the ranked alternatives shown when a
            book move is played; an out-of-book position yields an empty list.
        """
        try:
            entries = [(entry.move, entry.weight) for entry in self._reader.find_all(board)]
        except Exception as exc:
            logger.warning("Opening-book listing failed for FEN %s: %s", board.fen(), exc)
            return []

        entries.sort(key=lambda item: item[1], reverse=True)
        return entries

    def has_moves(self, board: chess.Board) -> bool:
        """Report whether the book contains any move for ``board``.

        Args:
            board: The position to look up.

        Returns:
            ``True`` if at least one legal book entry exists for the position,
            ``False`` otherwise (including on any read error).
        """
        try:
            return next(self._reader.find_all(board), None) is not None
        except Exception as exc:
            logger.warning("Opening-book lookup failed for FEN %s: %s", board.fen(), exc)
            return False

    def close(self) -> None:
        """Close the underlying reader, releasing the memory map.

        Safe to call more than once; any error while closing is logged at debug
        level and swallowed so shutdown is never interrupted.
        """
        try:
            self._reader.close()
        except Exception as exc:
            logger.debug("Error while closing opening-book reader: %s", exc)

    def __enter__(self) -> "OpeningBook":
        """Enter the runtime context and return this instance."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit the runtime context, closing the reader."""
        self.close()

    def __repr__(self) -> str:
        return f"OpeningBook(path={str(self._path)!r})"


def load_book(path: str | os.PathLike[str] | None = None) -> OpeningBook | None:
    """Open the Polyglot opening book and wrap it in an :class:`OpeningBook`.

    Args:
        path: Path to the Polyglot ``.bin`` file. Defaults to
            :data:`chess_ai.config.OPENING_BOOK_PATH` when ``None``.

    Returns:
        An :class:`OpeningBook` when the file exists, is non-empty, and opens
        cleanly; otherwise ``None``. A missing, empty, or corrupt book is logged
        and degraded to ``None`` rather than raised, so application startup is
        never blocked by an absent optional artifact.
    """
    book_path = Path(path) if path is not None else Path(OPENING_BOOK_PATH)

    if not book_path.is_file():
        logger.warning(
            "Opening book not found at %s; the AI will search without a book.",
            book_path,
        )
        return None

    if book_path.stat().st_size == 0:
        logger.warning(
            "Opening book at %s is empty; the AI will search without a book.",
            book_path,
        )
        return None

    try:
        reader = chess.polyglot.open_reader(str(book_path))
    except Exception as exc:
        logger.warning(
            "Failed to open opening book at %s (%s); continuing without a book.",
            book_path,
            exc,
        )
        return None

    logger.info("Loaded opening book from %s (%d bytes).", book_path, book_path.stat().st_size)
    return OpeningBook(reader, book_path)


__all__ = ["OpeningBook", "load_book"]
