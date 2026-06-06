"""Tactical search test suite for the hand-built chess engine (Constraint 11).

This suite exercises :mod:`chess_ai.engine.search` against ten pre-validated FEN
positions covering the exact mix the project requires:

* three mate-in-1 positions,
* two mate-in-2 positions,
* two hanging-piece (win-material) positions,
* two passed-pawn positions, and
* one stalemate-avoidance position.

The engine under test is a pure, synchronous computation library, so these are
plain unit tests: no ``asyncio``, no FastAPI, and no WebSocket test client are
involved. Each search runs on a freshly built
:class:`~chess_ai.engine.search.Searcher` (via :func:`make_searcher`) so the
transposition table, killer moves, and history heuristic can never leak between
unrelated positions.

Every FEN string is used verbatim. The positions were confirmed with
python-chess for legality, exact mate distance, undefended victims, and the
single stalemating move, so a failing assertion signals an engine regression
rather than an invalid position. Mate scores are checked against
:data:`MATE_THRESHOLD` rather than exact values, which keeps the suite robust to
the precise ply-distance encoding of a forced mate.
"""

import chess
import pytest
from conftest import START_FEN

from chess_ai.config import get_tier
from chess_ai.engine.evaluator import Evaluator
from chess_ai.engine.move_order import MoveOrderer
from chess_ai.engine.search import MATE, Searcher, SearchLimits

# A correct engine returns a mate-range score for a forced mate. Mate scores are
# encoded close to MATE (``MATE - ply_to_mate``), so this generous lower bound
# clears both a mate-in-1 (``MATE - 1``) and a mate-in-2 (``MATE - 3``) while
# staying far above any plausible material-only advantage.
MATE_THRESHOLD = MATE - 1000


def make_searcher() -> Searcher:
    """Build a fresh, fully wired searcher for a single position.

    A new :class:`~chess_ai.engine.search.Searcher` is constructed on every call,
    each owning its own :class:`~chess_ai.engine.evaluator.Evaluator` and
    :class:`~chess_ai.engine.move_order.MoveOrderer`. Because nothing is shared,
    the transposition table, killer moves, and history heuristic all start empty
    for every tactical position and cannot bleed between unrelated searches.

    Returns:
        A ready-to-use :class:`~chess_ai.engine.search.Searcher`.
    """
    return Searcher(evaluator=Evaluator(), move_orderer=MoveOrderer())


def best(fen: str, depth: int = 6, time_s: float = 10.0):
    """Run a fresh, fixed-depth search on ``fen`` and return its ``SearchResult``.

    A new searcher is created per call so each position is searched in isolation.
    The depth drives the result; the generous ``time_s`` budget is only a safety
    cap so a runaway search cannot hang the suite.

    Args:
        fen: The position to search, in Forsyth-Edwards Notation.
        depth: The fixed iterative-deepening depth in plies.
        time_s: The wall-clock budget in seconds (a safety cap, not the driver).

    Returns:
        The ``SearchResult`` produced by the search.
    """
    board = chess.Board(fen)
    limits = SearchLimits(depth=depth, time_budget_s=time_s)
    return make_searcher().search(board, limits)


def leads_to_checkmate(fen: str, move: chess.Move) -> bool:
    """Return ``True`` if playing ``move`` in ``fen`` delivers checkmate.

    Args:
        fen: The position before the move, in Forsyth-Edwards Notation.
        move: The move to play.

    Returns:
        ``True`` when the resulting position is checkmate, otherwise ``False``.
    """
    board = chess.Board(fen)
    board.push(move)
    return board.is_checkmate()


# ---------------------------------------------------------------------------
# 3 mate-in-1 positions (White to move; each is a unique forced mate)
# ---------------------------------------------------------------------------
MATE_IN_1 = [
    ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "a1a8"),  # Ra8#
    ("k7/7R/1K6/8/8/8/8/8 w - - 0 1", "h7h8"),  # Rh8#
    ("6k1/5ppp/8/8/8/8/8/4R1K1 w - - 0 1", "e1e8"),  # Re8#
]


@pytest.mark.parametrize(("fen", "uci"), MATE_IN_1, ids=["mate1_ra8", "mate1_rh8", "mate1_re8"])
def test_finds_mate_in_one(fen: str, uci: str) -> None:
    """The engine plays the unique mating move and reports a mate-range score."""
    result = best(fen, depth=3)
    assert result.best_move is not None
    # Primary check: the chosen move actually delivers checkmate.
    assert leads_to_checkmate(fen, result.best_move)
    # Secondary: the score is in the mate range (positive = side to move winning).
    assert result.score_cp >= MATE_THRESHOLD
    # Each position has a unique mate, so the exact move is assertable too.
    assert result.best_move == chess.Move.from_uci(uci)


# ---------------------------------------------------------------------------
# 2 mate-in-2 positions (White to move; forced mate, verified no mate-in-1)
# ---------------------------------------------------------------------------
MATE_IN_2 = [
    "8/8/8/8/3Q4/k7/8/1K6 w - - 0 1",  # KQK, forced mate in 2
    "8/8/8/8/2R5/k7/8/1K6 w - - 0 1",  # KRK, forced mate in 2
]


@pytest.mark.parametrize("fen", MATE_IN_2, ids=["mate2_kqk", "mate2_krk"])
def test_finds_mate_in_two(fen: str) -> None:
    """The engine finds the forced mate (needs depth >= 4; depth 6 is instant here)."""
    result = best(fen, depth=6)
    assert result.best_move is not None
    assert result.best_move in chess.Board(fen).legal_moves
    # Both positions are verified forced mates with no mate-in-1, so a correct
    # engine must report a mate-range score at this depth.
    assert result.score_cp >= MATE_THRESHOLD


# ---------------------------------------------------------------------------
# 2 hanging-piece positions (White to move; win an undefended piece)
# ---------------------------------------------------------------------------
HANGING = [
    # Black queen on h4 is undefended; Nf3xh4 wins it outright.
    ("rnb1kbnr/pppp1ppp/8/4p3/7q/5N2/PPPPPPPP/RNBQKB1R w KQkq - 0 1", "f3h4", 200),
    # Black rook on d5 is undefended; Qd2xd5 wins it.
    ("4k3/8/8/3r4/8/8/3Q4/4K3 w - - 0 1", "d2d5", 200),
]


@pytest.mark.parametrize(("fen", "uci", "min_cp"), HANGING, ids=["hang_queen_h4", "hang_rook_d5"])
def test_wins_hanging_piece(fen: str, uci: str, min_cp: int) -> None:
    """The engine captures the free piece and recognizes the won material."""
    result = best(fen, depth=5)
    expected = chess.Move.from_uci(uci)
    # Capturing the undefended piece is uniquely best.
    assert result.best_move == expected
    # The score reflects the won material (clearly positive for the side to move).
    assert result.score_cp > min_cp


# ---------------------------------------------------------------------------
# 2 passed-pawn positions (White to move; advance / promote the passer)
# ---------------------------------------------------------------------------
def test_passed_pawn_promotes() -> None:
    """The engine promotes the passed c-pawn (c8=Q+) and sees the won material."""
    # White pawn on c7 promotes with check; the lone black king on h3 is far away.
    fen = "8/2P5/8/8/8/7k/8/K7 w - - 0 1"
    result = best(fen, depth=5)
    assert result.best_move is not None
    assert result.best_move.from_square == chess.C7
    assert result.best_move.promotion == chess.QUEEN
    assert result.score_cp > 200  # up roughly a queen's worth of material


def test_passed_pawn_pushes() -> None:
    """The engine advances the winning b-pawn passer (b6-b7)."""
    # The black king on h2 cannot catch the b-pawn, so pushing it is best.
    fen = "8/8/1P6/8/8/8/7k/K7 w - - 0 1"
    result = best(fen, depth=6)
    assert result.best_move is not None
    assert result.best_move.from_square == chess.B6
    # Within the search horizon the pawn queens, so the score is clearly winning.
    assert result.score_cp > 150


# ---------------------------------------------------------------------------
# 1 stalemate-avoidance position (White to move; keep the win, do not stalemate)
# ---------------------------------------------------------------------------
def test_avoids_stalemate() -> None:
    """The engine keeps the win instead of stalemating the lone black king."""
    # White Qg7 + Ke6 vs lone black Ka8: White is up a queen. Exactly one move
    # stalemates (Qc7 = g7c7) and there is no mate-in-1, so a correct engine must
    # avoid the trap and keep its winning advantage.
    fen = "k7/6Q1/4K3/8/8/8/8/8 w - - 0 1"
    result = best(fen, depth=6)
    assert result.best_move is not None
    board = chess.Board(fen)
    board.push(result.best_move)
    assert not board.is_stalemate()  # avoided the stalemate trap
    assert result.best_move != chess.Move.from_uci("g7c7")  # specifically not Qc7
    assert result.score_cp > 300  # kept the winning advantage


# ---------------------------------------------------------------------------
# Structural / contract tests (not part of the Constraint 11 tactical mix)
# ---------------------------------------------------------------------------
def test_search_returns_legal_move(make_board) -> None:
    """A start-position search returns a legal move with a well-formed result."""
    result = best(START_FEN, depth=4)
    board = make_board()
    assert result.best_move in board.legal_moves
    assert isinstance(result.score_cp, int)
    assert result.depth >= 1
    assert result.nodes > 0
    assert result.time_s >= 0.0


def test_search_pv_starts_with_best_move() -> None:
    """A non-terminal search yields a non-empty PV that begins with the best move.

    The start position is non-terminal, so a depth-4 search must return a
    principal variation; an empty PV is a regression, not an allowed outcome.
    """
    result = best(START_FEN, depth=4)
    assert result.pv, "non-terminal search must return a non-empty principal variation"
    assert result.pv[0] == result.best_move


def test_search_from_tier_easy(make_board) -> None:
    """``SearchLimits.from_tier`` maps the Easy tier to depth 4 and searches legally."""
    limits = SearchLimits.from_tier(get_tier("easy"))
    assert limits.depth == 4
    board = make_board()
    result = make_searcher().search(board, limits)
    assert result.best_move in board.legal_moves


def test_search_handles_terminal_position() -> None:
    """A position that is already checkmate yields no move (``best_move is None``)."""
    # Fool's mate: White is checkmated, so there is no legal move to search.
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1"
    board = chess.Board(fen)
    assert board.is_checkmate()
    result = best(fen, depth=4)
    assert result.best_move is None
