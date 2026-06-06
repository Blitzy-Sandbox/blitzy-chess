"""Unit tests for the static evaluator (``chess_ai.engine.evaluator``).

This suite verifies the pure-computation static evaluation function and the
tuning data it consumes from :mod:`chess_ai.engine.tables`. The evaluator is a
web-framework-free module (Constraint 3), so the tests import and exercise it
directly with no FastAPI/async machinery.

Two sign conventions are tested and must never be conflated -- mixing them is the
single most common evaluator-test mistake:

* :meth:`Evaluator.evaluate` returns a scalar in centipawns from the
  **side-to-move** point of view (the negamax convention used by the search):
  positive means the player to move is better.
* :meth:`Evaluator.evaluate_components` returns the per-term breakdown from
  **White's** point of view: positive favors White regardless of whose turn it
  is.

What is asserted (and, deliberately, what is not):

* The assertions check *signs*, *ranges*, *monotonic relationships*, *mirror
  symmetry*, and *cache invariances* -- properties that are robust to the engine
  author re-tuning the piece-square tables. Exact piece-square-derived
  centipawn magnitudes are intentionally NOT asserted, because they are brittle.
* Material magnitudes are cross-checked against the engine's own
  :data:`chess_ai.engine.tables.PIECE_VALUES` rather than hard-coded numbers, so
  the tests track the engine's data instead of duplicating it.

Coverage maps to the evaluator's contract and to AAP constraints 4 and 5:

* Material sign in both points of view.
* Game phase: ``PHASE_MAX`` (24) at the start, ``0`` with bare kings, always in
  ``[0, 24]``, and monotonically lower once queens are removed (Constraint 4).
* The pawn-only Zobrist key: it changes when a pawn moves, is stable when only
  pieces move, and differs from the full-board ``chess.polyglot.zobrist_hash``
  (Constraint 5).
* Component-sum consistency and color-mirror antisymmetry.
* Cache determinism: repeated evaluation is identical and surviving a
  :meth:`Evaluator.clear_cache` does not change the score.

The suite is fully synchronous and deterministic; the ``ev`` fixture hands every
test a fresh :class:`Evaluator` with empty caches.
"""

from collections.abc import Callable

import chess
import chess.polyglot
import pytest

from chess_ai.engine import tables
from chess_ai.engine.evaluator import EvalComponents, Evaluator, pawn_key

# A board factory: the conftest ``make_board`` fixture returns a callable that
# builds a ``chess.Board`` from an optional FEN (``None`` -> starting position).
BoardFactory = Callable[..., chess.Board]

# ---------------------------------------------------------------------------
# Shared test positions. Each FEN is a legal python-chess position; the
# behaviour each one drives is noted alongside it.
# ---------------------------------------------------------------------------
# Black is missing its queen (d8 empty): White is up a full queen. Drives the
# material-sign and lopsided-total checks.
QUEEN_UP_FEN = "rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Black is missing its a8 rook (so no queenside castling for Black): White is up
# a rook. Provided in both turn framings to exercise the side-to-move sign flip.
ROOK_UP_WHITE_TO_MOVE_FEN = "1nbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQk - 0 1"
ROOK_UP_BLACK_TO_MOVE_FEN = "1nbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQk - 0 1"

# Only the two kings remain: a pure endgame with phase 0.
BARE_KINGS_FEN = "8/8/8/4k3/8/4K3/8/8 w - - 0 1"

# The standard array with both queens removed (d1 and d8 empty): used for the
# monotonic phase-decrease check (two queens => phase drops by 2 * 4 = 8).
QUEENLESS_FEN = "rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR w KQkq - 0 1"

# A king-and-pawn endgame (one White pawn): non-trivial position with phase 0.
KP_ENDGAME_FEN = "8/5k2/8/8/3K4/8/4P3/8 w - - 0 1"

# A quiet Italian-Game middlegame with the full complement of pieces.
MIDDLEGAME_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"

# A symmetric, piece-rich middlegame used as a mirror-symmetry probe.
IMBALANCED_MIDDLEGAME_FEN = (
    "r2q1rk1/ppp2ppp/2np1n2/2b1p1B1/2B1P1b1/2NP1N2/PPP2PPP/R2Q1RK1 w - - 0 1"
)

# A rook-versus-bare-king endgame: another mirror-symmetry probe with a clear
# White advantage.
ROOK_ENDGAME_FEN = "8/5k2/8/8/8/8/4R3/4K3 w - - 0 1"

# ---------------------------------------------------------------------------
# Tolerances. The evaluator is integer-valued and has no randomness; the only
# slack comes from floored phase interpolation, which breaks exact color
# antisymmetry by at most a couple of centipawns.
# ---------------------------------------------------------------------------
# The starting position has no tempo term in this engine, so its score is 0; the
# tolerance leaves headroom for a small future tempo bonus without going brittle.
STARTPOS_TOLERANCE_CP = 50

# Color-mirror totals should cancel; allow a few centipawns for floor-division
# rounding in the tapered positional and king-safety terms (measured worst case
# is 2 cp).
MIRROR_TOLERANCE_CP = 8


@pytest.fixture
def ev() -> Evaluator:
    """Return a fresh :class:`Evaluator` with empty pawn and evaluation caches."""
    return Evaluator()


# ---------------------------------------------------------------------------
# Material and sign convention
# ---------------------------------------------------------------------------
def test_startpos_is_balanced(ev: Evaluator) -> None:
    """The symmetric starting position is materially even and scores near zero."""
    board = chess.Board()
    components = ev.evaluate_components(board)

    # Both sides hold identical forces, so the WHITE-POV material term is exactly 0.
    assert components.material == 0
    # The full WHITE-POV total is symmetric at the start; it is at most a small
    # tolerance away from zero.
    assert abs(components.total) <= STARTPOS_TOLERANCE_CP
    # evaluate() is side-to-move POV; White moves first, so it tracks the total
    # and is likewise near zero.
    assert abs(ev.evaluate(board)) <= STARTPOS_TOLERANCE_CP


def test_extra_white_queen_favors_white(ev: Evaluator) -> None:
    """A position with Black missing its queen favors White in both points of view."""
    board = chess.Board(QUEEN_UP_FEN)
    components = ev.evaluate_components(board)

    # WHITE-POV material is positive and on the order of a queen. The expected
    # magnitude is sourced from the engine's own piece-value table (no magic
    # number) and given 100 cp of slack so the check survives PST re-tuning.
    assert components.material > 0
    assert components.material >= tables.PIECE_VALUES[chess.QUEEN] - 100
    # Side-to-move POV: White is to move and a queen up, so evaluate() > 0.
    assert ev.evaluate(board) > 0


def test_side_to_move_sign_flips(ev: Evaluator) -> None:
    """evaluate() follows the side to move; evaluate_components() stays WHITE-POV."""
    white_to_move = chess.Board(ROOK_UP_WHITE_TO_MOVE_FEN)
    black_to_move = chess.Board(ROOK_UP_BLACK_TO_MOVE_FEN)

    # White is up a rook. With White to move the side-to-move score is positive.
    assert ev.evaluate(white_to_move) > 0
    # The identical material edge with Black to move scores negative: the side to
    # move (Black) is the one who is worse off.
    assert ev.evaluate(black_to_move) < 0

    # evaluate_components is WHITE POV, so its material sign does NOT depend on
    # whose turn it is -- White is up a rook in both framings, and the value is
    # identical because material ignores the side to move entirely.
    white_pov_when_white_moves = ev.evaluate_components(white_to_move).material
    white_pov_when_black_moves = ev.evaluate_components(black_to_move).material
    assert white_pov_when_white_moves > 0
    assert white_pov_when_black_moves > 0
    assert white_pov_when_white_moves == white_pov_when_black_moves


# ---------------------------------------------------------------------------
# Game phase (Constraint 4: tapered evaluation over a material phase 0..24)
# ---------------------------------------------------------------------------
def test_phase_startpos_is_24(ev: Evaluator) -> None:
    """The starting position is full midgame: phase == PHASE_MAX (24)."""
    assert ev.phase(chess.Board()) == tables.PHASE_MAX
    assert ev.phase(chess.Board()) == 24


def test_phase_bare_kings_is_zero(ev: Evaluator) -> None:
    """With only the two kings on the board the phase is 0 (pure endgame)."""
    assert ev.phase(chess.Board(BARE_KINGS_FEN)) == 0


def test_phase_in_range(ev: Evaluator, make_board: BoardFactory) -> None:
    """Across opening, middlegame, and endgame positions the phase stays in [0, 24]."""
    for fen in (None, MIDDLEGAME_FEN, QUEENLESS_FEN, KP_ENDGAME_FEN, BARE_KINGS_FEN):
        board = make_board(fen)
        phase = ev.phase(board)
        assert 0 <= phase <= tables.PHASE_MAX


def test_phase_monotonic_decrease(ev: Evaluator) -> None:
    """Removing both queens lowers the material phase by exactly their weight."""
    start_phase = ev.phase(chess.Board())
    queenless_phase = ev.phase(chess.Board(QUEENLESS_FEN))

    assert start_phase > queenless_phase
    # Two queens, each weighted 4 by PHASE_WEIGHTS, account for the whole drop.
    assert start_phase - queenless_phase == 2 * tables.PHASE_WEIGHTS[chess.QUEEN]


# ---------------------------------------------------------------------------
# Pawn-only Zobrist key (Constraint 5: pawn structure cached on a pawn-only key)
# ---------------------------------------------------------------------------
def test_pawn_key_is_int() -> None:
    """pawn_key returns a Python int (a 64-bit Zobrist value)."""
    assert isinstance(pawn_key(chess.Board()), int)


def test_pawn_key_changes_when_pawn_moves() -> None:
    """Advancing a pawn changes the pawn skeleton and therefore the pawn key."""
    board = chess.Board()
    before = pawn_key(board)
    board.push_uci("e2e4")
    assert pawn_key(board) != before


def test_pawn_key_stable_when_only_pieces_move() -> None:
    """Moving only knights leaves the pawn key equal to the starting key.

    The pawn-only key depends on pawn placement alone (Constraint 5): it ignores
    every non-pawn piece, the side to move, castling rights, and en passant. A
    position reached from the start by knight moves has an identical pawn set, so
    its pawn key must equal the start's.
    """
    start_key = pawn_key(chess.Board())
    board = chess.Board()
    board.push_uci("g1f3")
    board.push_uci("b8c6")
    assert pawn_key(board) == start_key


def test_pawn_key_differs_from_full_zobrist() -> None:
    """The pawn-only key is distinct from the full-board Polyglot Zobrist hash."""
    board = chess.Board()
    assert pawn_key(board) != chess.polyglot.zobrist_hash(board)


# ---------------------------------------------------------------------------
# Component breakdown and color-mirror symmetry
# ---------------------------------------------------------------------------
def test_components_total_consistency(ev: Evaluator, make_board: BoardFactory) -> None:
    """``total`` equals the sum of the five terms, and ``phase`` is in range."""
    for fen in (None, QUEEN_UP_FEN, MIDDLEGAME_FEN, KP_ENDGAME_FEN):
        components = ev.evaluate_components(make_board(fen))
        assert isinstance(components, EvalComponents)

        component_sum = (
            components.material
            + components.positional
            + components.pawns
            + components.king_safety
            + components.mobility
        )
        assert components.total == component_sum
        assert 0 <= components.phase <= tables.PHASE_MAX


def test_total_sign_in_lopsided_position(ev: Evaluator) -> None:
    """In a clearly winning position the WHITE-POV total matches the material sign."""
    components = ev.evaluate_components(chess.Board(QUEEN_UP_FEN))
    assert components.material > 0
    assert components.total > 0


def test_mirror_symmetry(ev: Evaluator) -> None:
    """A position and its color mirror evaluate to opposite WHITE-POV scores.

    ``Board.mirror()`` swaps colors and flips the board vertically, so a perfectly
    symmetric evaluator returns exactly negated WHITE-POV components. Material is
    exactly antisymmetric here; the full total is allowed a few centipawns of
    slack for floor-division rounding in the tapered terms.
    """
    for fen in (MIDDLEGAME_FEN, IMBALANCED_MIDDLEGAME_FEN, ROOK_ENDGAME_FEN):
        board = chess.Board(fen)
        mirror = board.mirror()
        original = ev.evaluate_components(board)
        flipped = ev.evaluate_components(mirror)

        # Material is exactly antisymmetric under a color mirror.
        assert original.material == -flipped.material
        # The full total is antisymmetric up to rounding from floored phase
        # interpolation in the positional and king-safety terms.
        assert abs(original.total + flipped.total) <= MIRROR_TOLERANCE_CP
        # Both sides have identical material, so the phase is mirror-invariant.
        assert original.phase == flipped.phase


# ---------------------------------------------------------------------------
# Caching behaviour (determinism and clear_cache safety)
# ---------------------------------------------------------------------------
def test_eval_is_deterministic_and_cache_safe(ev: Evaluator) -> None:
    """Repeated evaluation is identical, and clearing the cache preserves the value."""
    board = chess.Board(MIDDLEGAME_FEN)

    first = ev.evaluate(board)
    second = ev.evaluate(board)  # may be served from the evaluation cache
    assert first == second

    ev.clear_cache()
    third = ev.evaluate(board)  # recomputed from scratch after the cache is cleared
    assert third == first


def test_clear_cache_no_error(ev: Evaluator) -> None:
    """clear_cache is safe on empty caches and after a real evaluation populates them."""
    # A brand-new evaluator has empty caches; clearing them must not raise.
    ev.clear_cache()

    # Populate the pawn and evaluation caches, then clear again.
    ev.evaluate(chess.Board())
    ev.clear_cache()
