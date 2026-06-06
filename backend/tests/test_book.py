"""Tests for the Polyglot opening book (``chess_ai.engine.book``).

This suite verifies the pure-computation opening-book layer that the searcher
probes before every move. It is organised around one load-bearing guarantee and
a set of best-effort positive-path checks:

* **Graceful-absent (mandatory, runs everywhere).** The Polyglot book is an
  OPTIONAL downloaded artifact (fetched by ``make init``) and is NOT present in
  CI/test environments. ``load_book`` for a missing, ``None``, empty, or corrupt
  path therefore MUST return ``None`` and MUST NEVER raise. These tests are the
  contract that holds in every environment and are written first.
* **Positive path (enforced).** Each positive-path test builds a tiny temporary
  Polyglot book in-process (``struct``-packed records keyed by
  :func:`chess.polyglot.zobrist_hash`) and then asserts that probing returns a
  legal weighted-random move, ``has_moves`` / ``list_moves`` report membership,
  weighted selection is deterministic under a seeded RNG, and ``close`` is
  idempotent. Because the book is generated locally with no external dependency,
  construction and loading MUST succeed; a failure is a product defect and fails
  the test rather than being hidden as a skip.

A Polyglot ``.bin`` file is a key-sorted sequence of 16-byte big-endian records
``key (uint64) | move (uint16) | weight (uint16) | learn (uint32)`` where the key
comes from :func:`chess.polyglot.zobrist_hash` -- the module-level Polyglot hash,
which is the correct key for a Polyglot book and is distinct from python-chess's
internal board hashing -- and the move is the bit-packed Polyglot move word.
python-chess ships no Polyglot writer, so the helpers below emit the records with
``struct`` and the encoding is validated by round-tripping through
``open_reader`` inside ``load_book`` itself.

Every test is synchronous and deterministic: temporary files use the ``tmp_path``
fixture (never the repository tree) and all randomness is seeded.
"""

import random
import struct
from pathlib import Path

import chess
import chess.polyglot

from chess_ai.engine.book import OpeningBook, load_book

# Polyglot promotion-piece codes, packed into bits 12-14 of the move word.
_PROMOTION_BITS: dict[int, int] = {
    chess.KNIGHT: 1,
    chess.BISHOP: 2,
    chess.ROOK: 3,
    chess.QUEEN: 4,
}


# ---------------------------------------------------------------------------
# Helpers: build a tiny, valid temporary Polyglot book for the positive path.
# ---------------------------------------------------------------------------
def _polyglot_move_bits(move: chess.Move) -> int:
    """Bit-pack ``move`` into the 16-bit Polyglot move encoding.

    Layout (least-significant bit first)::

        bits 0-2   to-file        bits 3-5   to-rank
        bits 6-8   from-file      bits 9-11  from-rank
        bits 12-14 promotion piece (0 = none, 1 = N, 2 = B, 3 = R, 4 = Q)
    """
    bits = (
        chess.square_file(move.to_square)
        | (chess.square_rank(move.to_square) << 3)
        | (chess.square_file(move.from_square) << 6)
        | (chess.square_rank(move.from_square) << 9)
    )
    if move.promotion is not None:
        bits |= _PROMOTION_BITS[move.promotion] << 12
    return bits


def _write_polyglot_book(path: Path, entries: list[tuple[chess.Board, chess.Move, int]]) -> None:
    """Write a minimal valid Polyglot ``.bin`` at ``path``.

    Args:
        path: Destination file (under the test's ``tmp_path``).
        entries: ``(board, move, weight)`` triples. The position key is derived
            with :func:`chess.polyglot.zobrist_hash`; records are sorted by key
            because Polyglot readers binary-search a key-sorted file.
    """
    records = [
        (chess.polyglot.zobrist_hash(board), _polyglot_move_bits(move), weight)
        for board, move, weight in entries
    ]
    records.sort(key=lambda record: record[0])
    payload = b"".join(
        struct.pack(">QHHI", key, move_bits, weight, 0) for key, move_bits, weight in records
    )
    path.write_bytes(payload)


def _load_temp_book(
    tmp_path: Path, entries: list[tuple[chess.Board, chess.Move, int]]
) -> OpeningBook:
    """Build and load a temporary, in-process Polyglot book.

    The book is generated entirely in-process: the records are ``struct``-packed
    and keyed with :func:`chess.polyglot.zobrist_hash`, both of which are pure
    Python with no external dependency or environment-specific behavior.
    Construction and loading must therefore SUCCEED, so any failure here -- a
    raised exception, or ``load_book`` returning ``None`` for a valid generated
    book -- is a product defect and is allowed to fail the test rather than be
    hidden as a skip. (Skips are reserved for the truly optional, downloaded real
    book artifact, which the graceful-absent contract tests cover separately.)
    """
    path = tmp_path / "book.bin"
    _write_polyglot_book(path, entries)
    book = load_book(str(path))
    assert book is not None, "load_book returned None for a valid generated Polyglot book"
    return book


# ---------------------------------------------------------------------------
# Phase 2 -- Graceful-absent (MANDATORY; these must pass in every environment).
# ---------------------------------------------------------------------------
def test_load_missing_book_returns_none():
    """A missing path yields ``None`` and never raises -- the everywhere contract."""
    assert load_book("/nonexistent/path/opening_book.bin") is None


def test_load_none_path_is_safe():
    """``load_book(None)`` falls back to the configured default and never raises.

    Returns ``None`` when no default book is installed (the usual CI/test case)
    or a valid :class:`OpeningBook` when a real default happens to be present;
    either way it must not raise.
    """
    book = load_book(None)
    assert book is None or hasattr(book, "probe")
    if book is not None:
        book.close()


def test_load_empty_file_returns_none(tmp_path: Path):
    """An empty book file degrades to ``None`` (no raise)."""
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert load_book(str(path)) is None


def test_load_garbage_file_is_safe(tmp_path: Path):
    """A corrupt (non-16-multiple) book degrades safely and never raises.

    A Polyglot reader may reject a malformed file outright or tolerate it; either
    way the loader must catch the error and return ``None``, or return a book that
    reports no moves. It must never propagate an exception.
    """
    path = tmp_path / "garbage.bin"
    path.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06")  # 7 bytes: not a multiple of 16.
    book = load_book(str(path))
    assert book is None or hasattr(book, "probe")
    if book is not None:
        assert book.has_moves(chess.Board()) is False
        book.close()


# ---------------------------------------------------------------------------
# Phase 3 -- Probe semantics when the book is absent (caller guard contract).
# ---------------------------------------------------------------------------
def test_probe_semantics_when_absent():
    """When the book is absent, ``load_book`` is ``None`` and callers must guard.

    This documents the calling contract used by the searcher and the WebSocket
    handler: ``book = load_book(...)`` followed by ``if book is not None:`` before
    any ``probe``. No method is invoked on a ``None`` book.
    """
    book = load_book("/nope.bin")
    assert book is None


# ---------------------------------------------------------------------------
# Phase 4 -- Positive path (best-effort: build a temp book or skip).
# ---------------------------------------------------------------------------
def test_probe_returns_legal_book_move(tmp_path: Path):
    """Probing a one-entry book returns that move, and it is legal in the position."""
    board = chess.Board()
    book = _load_temp_book(tmp_path, [(board, chess.Move.from_uci("e2e4"), 10)])
    try:
        move = book.probe(board, rng=random.Random(0))
        assert move is None or board.is_legal(move)
        assert move == chess.Move.from_uci("e2e4")
    finally:
        book.close()


def test_has_moves_and_list_moves(tmp_path: Path):
    """``has_moves`` / ``list_moves`` report membership; off-book positions are empty."""
    start = chess.Board()
    book = _load_temp_book(tmp_path, [(start, chess.Move.from_uci("e2e4"), 10)])
    try:
        assert book.has_moves(start) is True

        entries = book.list_moves(start)
        assert any(move == chess.Move.from_uci("e2e4") for move, _weight in entries)
        assert all(weight > 0 for _move, weight in entries)

        # A position the one-entry book does not contain.
        off_book = chess.Board()
        off_book.push(chess.Move.from_uci("e2e4"))
        assert book.has_moves(off_book) is False
        assert book.probe(off_book, rng=random.Random(0)) is None
    finally:
        book.close()


def test_weighted_choice_is_deterministic_with_seed(tmp_path: Path):
    """Identical seeds select identical moves; both weighted moves occur across seeds."""
    start = chess.Board()
    e2e4 = chess.Move.from_uci("e2e4")
    d2d4 = chess.Move.from_uci("d2d4")
    book = _load_temp_book(tmp_path, [(start, e2e4, 30), (start, d2d4, 10)])
    try:
        # Determinism: two freshly-seeded RNGs produce the same move.
        first = book.probe(start, rng=random.Random(42))
        second = book.probe(start, rng=random.Random(42))
        assert first == second

        # Weighting: across a fixed seed range both legal book moves appear, and
        # every selection is a legal move (never ``None`` for an in-book position).
        chosen = {book.probe(start, rng=random.Random(seed)) for seed in range(50)}
        assert all(move is not None and start.is_legal(move) for move in chosen)
        assert e2e4 in chosen
        assert d2d4 in chosen
    finally:
        book.close()


def test_close_is_safe(tmp_path: Path):
    """``close`` runs cleanly and is idempotent (a second close must not raise)."""
    book = _load_temp_book(tmp_path, [(chess.Board(), chess.Move.from_uci("e2e4"), 10)])
    book.close()
    book.close()
