"""Pure chess AI engine: evaluation, search, move ordering, opening book, and endgame tablebases.

Re-exports the public API of the engine submodules so consumers can import them
from ``chess_ai.engine`` directly: the search core from ``search``
(``Searcher``, ``SearchLimits``, ``SearchResult``, ``SearchInfo``,
``TranspositionTable``, ``find_best_move``), static evaluation from ``evaluator``
(``Evaluator``, ``EvalComponents``), move ordering from ``move_order``
(``MoveOrderer``), the Polyglot opening book from ``book`` (``OpeningBook``,
``load_book``), and the Syzygy endgame tablebase from ``endgame``
(``EndgameTablebase``, ``open_tablebase``).

This package is a pure computation library: it imports ``chess`` (python-chess)
and the standard library transitively through its submodules, and never imports
FastAPI, Starlette, uvicorn, asyncio, or any WebSocket transport code. It can
therefore run inside a worker thread and be unit-tested in isolation.
"""

from chess_ai.engine.book import OpeningBook, load_book
from chess_ai.engine.endgame import EndgameTablebase, open_tablebase
from chess_ai.engine.evaluator import EvalComponents, Evaluator
from chess_ai.engine.move_order import MoveOrderer
from chess_ai.engine.search import (
    Searcher,
    SearchInfo,
    SearchLimits,
    SearchResult,
    TranspositionTable,
    find_best_move,
)

__all__ = [
    # Search core (search).
    "Searcher",
    "SearchLimits",
    "SearchResult",
    "SearchInfo",
    "TranspositionTable",
    "find_best_move",
    # Static evaluation (evaluator).
    "Evaluator",
    "EvalComponents",
    # Move ordering (move_order).
    "MoveOrderer",
    # Polyglot opening book (book).
    "OpeningBook",
    "load_book",
    # Syzygy endgame tablebase (endgame).
    "EndgameTablebase",
    "open_tablebase",
]
