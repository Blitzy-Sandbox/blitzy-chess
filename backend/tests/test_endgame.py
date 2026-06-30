"""Tests for Syzygy endgame tablebase probing (``chess_ai.engine.endgame``).

This suite verifies the pure-computation Syzygy layer the searcher short-circuits
to when six or fewer pieces remain. It is organised around two load-bearing
guarantees that hold in *every* environment, plus a set of best-effort probe
checks that run only when the optional tables are installed:

* **Graceful-absent (mandatory, runs everywhere).** The Syzygy tables are an
  OPTIONAL downloaded artifact (fetched by ``make download-syzygy``) and are NOT
  present in CI/test environments. :func:`~chess_ai.engine.endgame.open_tablebase`
  for a missing, empty, ``None``, or non-directory path therefore MUST return
  ``None`` and MUST NEVER raise. These tests are the contract that holds in every
  environment and are written first.
* **Piece-count gating (pure, runs everywhere).**
  :meth:`~chess_ai.engine.endgame.EndgameTablebase.should_probe` is pure
  arithmetic over :func:`chess.popcount` and never touches the wrapped handle, so
  the ``<= MAX_TABLEBASE_PIECES`` boundary is exercised directly with a stub
  reader -- no real tables required.
* **Probe semantics (best-effort).** When real tables are installed, probing a
  winning position returns a positive WDL, the chosen move is legal, and
  :meth:`~chess_ai.engine.endgame.EndgameTablebase.probe_best_move` leaves the
  board unchanged (its ``push``/``pop`` stack stays balanced). Each of these
  ``pytest.skip``\\ s when no tablebase is available, so the mandatory contract
  above still passes everywhere.

Because ``should_probe`` is a pure piece count and the probe entry points
short-circuit on that count (and otherwise catch the "position not covered"
errors), a tiny stub reader lets the gating, miss-path, push/pop balance, and
``close`` behaviors be asserted deterministically with zero tables installed.

Every test is synchronous. Temporary directories use the ``tmp_path`` fixture
(never the repository tree).
"""

from pathlib import Path

import chess
import pytest

from chess_ai.engine.endgame import MAX_TABLEBASE_PIECES, EndgameTablebase, open_tablebase

# ---------------------------------------------------------------------------
# Test positions (piece counts verified with chess.popcount(board.occupied)).
# ---------------------------------------------------------------------------
# KQ vs k -- three pieces, at or below the ceiling, so probe-eligible. This exact
# layout leaves the side-not-to-move in check (chess.Board.is_valid() is False),
# which is irrelevant to should_probe's pure piece count; it is used only for the
# gating assertions, never for an actual probe.
_SMALL_ENDGAME_FEN = "8/8/8/4k3/8/8/4Q3/4K3 w - - 0 1"

# KQ vs K, White to move -- a clean, fully legal (is_valid()) winning position
# with 26 legal moves. Used by the dummy push/pop balance test and by the
# skip-guarded real-table probe tests.
_WINNING_KQK_FEN = "4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1"

# K + four pawns vs k -- exactly six pieces, the inclusive ceiling, so eligible.
_SIX_PIECE_FEN = "4k3/8/8/8/8/8/3PPPP1/4K3 w - - 0 1"

# K + five pawns vs k -- seven pieces, one above the ceiling, so NOT eligible.
_SEVEN_PIECE_FEN = "4k3/8/8/8/8/8/2PPPPP1/4K3 w - - 0 1"


# ---------------------------------------------------------------------------
# Stub readers: stand in for a chess.syzygy.Tablebase handle with no tables.
# ---------------------------------------------------------------------------
class _DummyReader:
    """A minimal stand-in for a python-chess Syzygy handle that has no tables.

    :class:`~chess_ai.engine.endgame.EndgameTablebase` only touches the wrapped
    handle inside :meth:`probe_wdl` / :meth:`probe_dtz` (each guarded by the
    module's "position not covered" error tuple) and :meth:`close`;
    :meth:`should_probe` is pure piece counting. This stub therefore lets the
    gating, miss-path, push/pop-balance, and close behaviors be exercised
    deterministically without any real Syzygy tables installed.

    Both probe methods raise :class:`KeyError` -- one of the engine's recognized
    "position not covered" errors (``chess.syzygy.MissingTableError`` is itself a
    :class:`KeyError` subclass) -- mirroring a reader that holds no table for the
    queried position. :meth:`close` records how many times it ran so idempotency
    can be asserted.
    """

    def __init__(self) -> None:
        self.close_calls = 0

    def probe_wdl(self, board: chess.Board) -> int:
        """Always raise, as if no table covers ``board``."""
        raise KeyError("dummy reader has no tables loaded")

    def probe_dtz(self, board: chess.Board) -> int:
        """Always raise, as if no table covers ``board``."""
        raise KeyError("dummy reader has no tables loaded")

    def close(self) -> None:
        """Record a close call (the wrapper may call this more than once)."""
        self.close_calls += 1


class _RaisingCloseReader:
    """A stub whose :meth:`close` raises, proving the wrapper swallows the error."""

    def close(self) -> None:
        """Raise to verify :meth:`EndgameTablebase.close` never propagates it."""
        raise RuntimeError("close failure must be swallowed by EndgameTablebase")


def _make_tb_without_tables(directory: str) -> EndgameTablebase:
    """Wrap a tableless :class:`_DummyReader` in an :class:`EndgameTablebase`."""
    return EndgameTablebase(_DummyReader(), directory)


def _maybe_tb() -> EndgameTablebase | None:
    """Open the configured Syzygy tablebase, or ``None`` when none is installed.

    The probe tests below call this and ``pytest.skip`` when it returns ``None``,
    so the suite stays green in the usual environment where the optional
    downloaded tables are absent.
    """
    from chess_ai import config

    return open_tablebase(getattr(config, "TABLES_DIR", None))


# ---------------------------------------------------------------------------
# Graceful-absent open (MANDATORY: must pass in every environment, no tables).
# ---------------------------------------------------------------------------
def test_open_missing_dir_returns_none():
    """A directory that does not exist degrades to ``None`` and never raises."""
    assert open_tablebase("/nonexistent/tables") is None


def test_open_empty_dir_returns_none(tmp_path: Path):
    """An existing but empty directory (no ``*.rtbw``/``*.rtbz``) yields ``None``."""
    assert open_tablebase(str(tmp_path)) is None


def test_open_none_is_safe():
    """``open_tablebase(None)`` falls back to the default dir and never raises.

    Returns ``None`` when no tables are installed (the usual CI/test case) or an
    :class:`EndgameTablebase` when a real default set happens to be present;
    either way it must not raise.
    """
    tb = open_tablebase(None)
    assert tb is None or hasattr(tb, "should_probe")
    if tb is not None:
        tb.close()


def test_open_file_path_returns_none(tmp_path: Path):
    """A path that exists but is a file (not a directory) degrades to ``None``."""
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("not a tablebase")
    assert open_tablebase(str(file_path)) is None


# ---------------------------------------------------------------------------
# Piece-count gating (pure arithmetic; always runs, needs NO tables).
# ---------------------------------------------------------------------------
def test_max_pieces_is_six():
    """The inclusive probe ceiling is six pieces, per the engine specification."""
    assert MAX_TABLEBASE_PIECES == 6


def test_should_probe_true_for_small_endgame(tmp_path: Path):
    """A three-piece endgame is below the ceiling, so probing is attempted."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_SMALL_ENDGAME_FEN)
    assert chess.popcount(board.occupied) <= MAX_TABLEBASE_PIECES
    assert tb.should_probe(board) is True


def test_should_probe_true_for_six_piece_boundary(tmp_path: Path):
    """Exactly six pieces is inclusive of the ceiling, so probing is attempted."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_SIX_PIECE_FEN)
    assert chess.popcount(board.occupied) == 6
    assert tb.should_probe(board) is True


def test_should_probe_false_for_startpos(tmp_path: Path):
    """The 32-piece start position is far above the ceiling: no probing."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board()
    assert chess.popcount(board.occupied) == 32
    assert tb.should_probe(board) is False


def test_should_probe_false_for_seven_pieces(tmp_path: Path):
    """Seven pieces is strictly above the inclusive six-piece ceiling: no probing."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_SEVEN_PIECE_FEN)
    assert chess.popcount(board.occupied) == 7
    assert tb.should_probe(board) is False


# ---------------------------------------------------------------------------
# Probe-path coverage without tables (stub reader; always runs).
# These exercise the real gating short-circuit, the "position not covered"
# error handling, the push/pop balance, and close semantics deterministically.
# ---------------------------------------------------------------------------
def test_probe_wdl_none_for_large_board(tmp_path: Path):
    """``probe_wdl`` gates on piece count and returns ``None`` before the reader."""
    tb = _make_tb_without_tables(str(tmp_path))
    # 32 pieces -> should_probe is False -> None without touching the dummy reader.
    assert tb.probe_wdl(chess.Board()) is None


def test_probe_dtz_none_for_large_board(tmp_path: Path):
    """``probe_dtz`` gates on piece count and returns ``None`` before the reader."""
    tb = _make_tb_without_tables(str(tmp_path))
    assert tb.probe_dtz(chess.Board()) is None


def test_probe_wdl_none_when_uncovered_small(tmp_path: Path):
    """A small position the tables cannot answer yields ``None``, not an error.

    The dummy reader raises :class:`KeyError`; the wrapper must catch it (it is a
    recognized "position not covered" error) and degrade to ``None``.
    """
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_WINNING_KQK_FEN)
    assert tb.probe_wdl(board) is None


def test_probe_dtz_none_when_uncovered_small(tmp_path: Path):
    """A small position the tables cannot answer yields ``None`` for DTZ too."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_WINNING_KQK_FEN)
    assert tb.probe_dtz(board) is None


def test_probe_best_move_none_and_balanced_with_dummy(tmp_path: Path):
    """``probe_best_move`` leaves the board unchanged and returns ``None`` here.

    The dummy reader raises for every child probe, so no move qualifies and the
    result is ``None``. The implementation pops every pushed move in a ``finally``
    block, so the board's FEN must be identical before and after -- the high-value
    non-mutation guarantee the searcher depends on.
    """
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_WINNING_KQK_FEN)
    before = board.fen()
    move = tb.probe_best_move(board)
    assert move is None
    assert board.fen() == before


def test_score_cp_none_when_uncovered(tmp_path: Path):
    """``score_cp`` returns ``None`` when the underlying WDL probe is uncovered."""
    tb = _make_tb_without_tables(str(tmp_path))
    board = chess.Board(_WINNING_KQK_FEN)
    assert tb.score_cp(board) is None


def test_close_idempotent_with_dummy(tmp_path: Path):
    """``close`` is safe to call repeatedly and delegates to the wrapped handle."""
    reader = _DummyReader()
    tb = EndgameTablebase(reader, str(tmp_path))
    tb.close()
    tb.close()
    assert reader.close_calls == 2


def test_close_swallows_underlying_error(tmp_path: Path):
    """``close`` swallows errors from the wrapped handle so shutdown never breaks."""
    tb = EndgameTablebase(_RaisingCloseReader(), str(tmp_path))
    tb.close()  # Must not raise even though the underlying close() does.


def test_context_manager_closes(tmp_path: Path):
    """Using the wrapper as a context manager closes the handle on exit."""
    reader = _DummyReader()
    with EndgameTablebase(reader, str(tmp_path)) as tb:
        assert tb.should_probe(chess.Board(_SMALL_ENDGAME_FEN)) is True
    assert reader.close_calls == 1


# ---------------------------------------------------------------------------
# Real-table probing (best-effort: SKIP when no Syzygy tables are installed).
# ---------------------------------------------------------------------------
def test_probe_wdl_when_available():
    """A winning position probes to a positive WDL when real tables are present.

    Skips when no Syzygy tables are installed (the usual CI/test case). When the
    installed set does not cover this specific position, ``probe_wdl`` returns
    ``None`` and only the result type is asserted.
    """
    tb = _maybe_tb()
    if tb is None:
        pytest.skip("no Syzygy tables installed")
    try:
        board = chess.Board(_WINNING_KQK_FEN)
        wdl = tb.probe_wdl(board)
        assert wdl is None or isinstance(wdl, int)
        if wdl is not None:
            assert wdl > 0  # White (the side to move) is winning in KQ vs K.
    finally:
        tb.close()


def test_probe_best_move_balanced_and_legal():
    """``probe_best_move`` returns a legal move (or ``None``) and never mutates.

    Skips when no Syzygy tables are installed.
    """
    tb = _maybe_tb()
    if tb is None:
        pytest.skip("no Syzygy tables installed")
    try:
        board = chess.Board(_WINNING_KQK_FEN)
        before = board.fen()
        move = tb.probe_best_move(board)
        assert board.fen() == before
        assert move is None or board.is_legal(move)
    finally:
        tb.close()


def test_probe_dtz_type():
    """``probe_dtz`` returns a signed integer or ``None``. Skips without tables."""
    tb = _maybe_tb()
    if tb is None:
        pytest.skip("no Syzygy tables installed")
    try:
        board = chess.Board(_WINNING_KQK_FEN)
        dtz = tb.probe_dtz(board)
        assert dtz is None or isinstance(dtz, int)
    finally:
        tb.close()


def test_close_real_tablebase_idempotent():
    """A real tablebase handle closes cleanly and idempotently. Skips without tables."""
    tb = _maybe_tb()
    if tb is None:
        pytest.skip("no Syzygy tables installed")
    tb.close()
    tb.close()  # Second close must not raise.
