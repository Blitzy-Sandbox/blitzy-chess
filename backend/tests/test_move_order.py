"""Unit tests for move ordering (``chess_ai.engine.move_order``).

This suite verifies the pure-computation move-ordering layer that feeds the
alpha-beta search: hash-move-first ordering, static exchange evaluation (SEE)
correctness and -- most importantly -- its non-mutation guarantee, MVV-LVA
victim ranking, and the killer / history cutoff bookkeeping.

Every position is a hand-built FEN whose tested capture has been confirmed
legal with python-chess, and every expectation is asserted as a relative
ordering or an arithmetic sign rather than a brittle, hard-coded full
permutation (which would couple the tests to incidental tie-break order). The
:class:`~chess_ai.engine.move_order.MoveOrderer` is a pure module -- it imports
no web-framework code -- so the tests construct and exercise it directly.

The suite is fully synchronous and deterministic: ``MoveOrderer`` seeds no
randomness, and where a behaviour depends on internal state the ``mo`` fixture
hands every test a fresh instance with empty killer and history tables.
"""

import chess
import pytest

from chess_ai.engine.move_order import MoveOrderer

# ---------------------------------------------------------------------------
# Shared test positions (the tested capture in each is verified legal below).
# ---------------------------------------------------------------------------
# White pawn e4 can capture the UNDEFENDED black queen on d5 -- a clearly
# winning capture (SEE is about +1 queen). Drives the positive-SEE and the
# load-bearing non-mutation checks.
WINNING_CAPTURE_FEN = "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1"

# White queen d1 can capture the black pawn on d5, but the black rook on d7
# recaptures: a clearly losing capture (SEE is about pawn minus queen, i.e.
# strongly negative).
LOSING_CAPTURE_FEN = "4k3/3r4/8/3p4/8/8/8/3QK3 w - - 0 1"

# Two captures are available to White: pawn e4 x queen d5 (most valuable
# victim, least valuable attacker) and rook a1 x pawn a7 (least valuable
# victim, more valuable attacker). Used to assert MVV-LVA ranks the
# pawn-takes-queen capture higher, and to exercise set-preservation on a
# capture-rich position.
MULTI_CAPTURE_FEN = "4k3/p7/8/3q4/4P3/8/8/R3K3 w - - 0 1"


@pytest.fixture
def mo() -> MoveOrderer:
    """Return a fresh :class:`MoveOrderer` with empty killer and history tables."""
    return MoveOrderer()


# ---------------------------------------------------------------------------
# Hash (transposition-table) move ordered first
# ---------------------------------------------------------------------------
def test_tt_move_is_first(mo: MoveOrderer) -> None:
    """The supplied hash move is placed first, with the move set otherwise intact."""
    board = chess.Board()
    tt_move = chess.Move.from_uci("g1f3")
    assert board.is_legal(tt_move)  # precondition: the hash move is legal here

    ordered = mo.order_moves(board, list(board.legal_moves), tt_move, 0)

    assert ordered[0] == tt_move
    assert len(ordered) == board.legal_moves.count()
    assert set(ordered) == set(board.legal_moves)


def test_order_moves_preserves_set(mo: MoveOrderer) -> None:
    """Ordering returns exactly the same multiset of moves it was handed."""
    board = chess.Board(MULTI_CAPTURE_FEN)
    legal = list(board.legal_moves)

    ordered = mo.order_moves(board, legal, None, 0)

    # No move dropped, none invented, none duplicated -- compare as sorted multisets.
    assert sorted(m.uci() for m in ordered) == sorted(m.uci() for m in legal)


def test_no_tt_move(mo: MoveOrderer) -> None:
    """With ``tt_move=None`` ordering still returns every legal move without raising."""
    board = chess.Board(MULTI_CAPTURE_FEN)
    legal = list(board.legal_moves)

    ordered = mo.order_moves(board, legal, None, 0)

    assert len(ordered) == len(legal)
    assert set(ordered) == set(legal)


# ---------------------------------------------------------------------------
# Static exchange evaluation (SEE)
# ---------------------------------------------------------------------------
def test_see_winning_capture_positive(mo: MoveOrderer) -> None:
    """Capturing an undefended queen with a pawn is a winning capture (SEE > 0)."""
    board = chess.Board(WINNING_CAPTURE_FEN)
    move = chess.Move.from_uci("e4d5")
    assert board.is_capture(move)  # precondition: the tested move is a capture

    assert mo.see(board, move) > 0


def test_see_equal_or_losing_capture(mo: MoveOrderer) -> None:
    """Capturing a rook-defended pawn with the queen loses material (SEE < 0)."""
    board = chess.Board(LOSING_CAPTURE_FEN)
    move = chess.Move.from_uci("d1d5")
    assert board.is_capture(move)  # precondition: the tested move is a capture

    assert mo.see(board, move) < 0


def test_see_does_not_mutate_board(mo: MoveOrderer) -> None:
    """SEE must leave the caller's board untouched -- no leaked pushes.

    This is the highest-value invariant in the suite: ``see`` is called during
    move ordering on the very board the search is exploring, so a SEE that
    pushed and forgot to pop (or mutated occupancy) would silently corrupt the
    search tree. ``see`` is required to operate purely on integer occupancy
    masks and never touch the board's move stack.
    """
    board = chess.Board(WINNING_CAPTURE_FEN)
    before_fen = board.fen()

    mo.see(board, chess.Move.from_uci("e4d5"))

    assert board.fen() == before_fen
    assert len(board.move_stack) == 0


def test_see_ge_threshold(mo: MoveOrderer) -> None:
    """``see_ge`` answers the threshold question and honours a custom threshold."""
    winning = chess.Board(WINNING_CAPTURE_FEN)
    losing = chess.Board(LOSING_CAPTURE_FEN)
    winning_move = chess.Move.from_uci("e4d5")
    losing_move = chess.Move.from_uci("d1d5")

    # Default threshold of 0: "is this capture at least equal?"
    assert mo.see_ge(winning, winning_move, 0) is True
    assert mo.see_ge(losing, losing_move, 0) is False
    # Winning a single queen does not clear a threshold set above a queen's value.
    assert mo.see_ge(winning, winning_move, 2000) is False


def test_mvv_lva_orders_high_value_victim_first(mo: MoveOrderer) -> None:
    """MVV-LVA scores most-valuable-victim / least-valuable-attacker highest."""
    if not hasattr(mo, "mvv_lva"):
        pytest.skip("mvv_lva is not part of the public MoveOrderer API")

    board = chess.Board(MULTI_CAPTURE_FEN)
    pawn_takes_queen = chess.Move.from_uci("e4d5")
    rook_takes_pawn = chess.Move.from_uci("a1a7")
    assert board.is_capture(pawn_takes_queen)  # precondition: both are captures
    assert board.is_capture(rook_takes_pawn)

    assert mo.mvv_lva(board, pawn_takes_queen) > mo.mvv_lva(board, rook_takes_pawn)


# ---------------------------------------------------------------------------
# Killer moves and the history heuristic
# ---------------------------------------------------------------------------
def test_record_cutoff_adds_quiet_to_killers(mo: MoveOrderer) -> None:
    """A quiet beta-cutoff move becomes this ply's killer and is ordered earlier."""
    board = chess.Board()
    killer = chess.Move.from_uci("g1f3")
    assert not board.is_capture(killer)  # precondition: the cutoff move is quiet

    # Baseline position of the move at this ply, before any cutoff is recorded.
    index_before = mo.order_moves(board, list(board.legal_moves), None, 2).index(killer)

    mo.record_cutoff(board, killer, depth=4, ply=2)

    index_after = mo.order_moves(board, list(board.legal_moves), None, 2).index(killer)

    # Recording the cutoff promotes the killer ahead of the other quiet moves.
    assert index_after < index_before
    # And, when the killer table is observable, the move occupies the first slot.
    if hasattr(mo, "killers"):
        assert mo.killers.get(2) is not None
        assert mo.killers.get(2)[0] == killer


def test_capture_not_stored_as_killer(mo: MoveOrderer) -> None:
    """Killers are quiet moves only: a capture cutoff must NOT populate a killer slot.

    Captures are already ordered by SEE / MVV-LVA, so recording one as a killer
    would double-count it. The contract is that ``record_cutoff`` ignores
    captures (and promotions) entirely.
    """
    board = chess.Board(WINNING_CAPTURE_FEN)
    capture = chess.Move.from_uci("e4d5")
    assert board.is_capture(capture)  # precondition: the cutoff move is a capture

    mo.record_cutoff(board, capture, depth=4, ply=3)

    if hasattr(mo, "killers"):
        # Probe with `in` / .get() rather than ``mo.killers[3]``: the latter would
        # materialise an empty slot through the backing defaultdict and mask the
        # real behaviour under test.
        assert 3 not in mo.killers
        assert mo.killers.get(3) is None
    else:
        pytest.skip("killer storage is not observable on the public API")


def test_history_increases_quiet_priority(mo: MoveOrderer) -> None:
    """Accumulated history credit orders a quiet move ahead of a historyless peer.

    History is keyed on ``(side_to_move, from, to)`` and is independent of ply,
    so the cutoffs are recorded at one ply and the effect is observed at a
    *different* ply where neither move is a killer. That isolates the history
    term from the killer term.
    """
    board = chess.Board()
    promoted = chess.Move.from_uci("g1f3")  # accrues history credit
    neutral = chess.Move.from_uci("b1c3")  # never recorded; stays at history 0
    assert not board.is_capture(promoted)  # precondition: both moves are quiet
    assert not board.is_capture(neutral)

    for depth in (2, 3, 4):
        mo.record_cutoff(board, promoted, depth=depth, ply=5)

    # Observe at a ply with no killer entry so that only history can reorder.
    ordered = mo.order_moves(board, list(board.legal_moves), None, 9)

    assert ordered.index(promoted) < ordered.index(neutral)
    if hasattr(mo, "history"):
        # depth*depth credit accumulated across the three recorded cutoffs.
        key = (board.turn, promoted.from_square, promoted.to_square)
        assert mo.history.get(key, 0) == 4 + 9 + 16


def test_clear_resets_state(mo: MoveOrderer) -> None:
    """``clear`` empties the killer and history tables and never raises."""
    board = chess.Board()
    mo.record_cutoff(board, chess.Move.from_uci("g1f3"), depth=4, ply=2)

    mo.clear()

    if hasattr(mo, "killers"):
        assert len(mo.killers) == 0
    if hasattr(mo, "history"):
        assert len(mo.history) == 0


# ---------------------------------------------------------------------------
# score_move banding sanity
# ---------------------------------------------------------------------------
def test_score_move_hash_move_highest(mo: MoveOrderer) -> None:
    """The hash move scores strictly higher than an ordinary quiet move."""
    board = chess.Board()
    tt_move = chess.Move.from_uci("g1f3")
    quiet = chess.Move.from_uci("b1c3")

    hash_score = mo.score_move(board, tt_move, tt_move, 0)
    quiet_score = mo.score_move(board, quiet, tt_move, 0)

    assert hash_score > quiet_score


def test_score_move_winning_capture_beats_quiet(mo: MoveOrderer) -> None:
    """A winning capture scores higher than a quiet, non-killer move."""
    board = chess.Board(WINNING_CAPTURE_FEN)
    winning_capture = chess.Move.from_uci("e4d5")
    quiet = chess.Move.from_uci("e1e2")  # legal, quiet king step (no history/killer)
    assert board.is_capture(winning_capture)  # precondition: capture vs. quiet
    assert board.is_legal(quiet) and not board.is_capture(quiet)

    capture_score = mo.score_move(board, winning_capture, None, 0)
    quiet_score = mo.score_move(board, quiet, None, 0)

    assert capture_score > quiet_score
