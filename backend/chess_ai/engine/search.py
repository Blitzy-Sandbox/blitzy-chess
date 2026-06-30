"""Alpha-beta chess search core (pure computation, synchronous).

This module is the search engine of the hand-built chess AI. It is a pure,
portable computation library: it imports only ``chess`` / ``chess.polyglot``,
the Python standard library, the sibling engine modules, and
``chess_ai.config``. It contains no web-framework, Starlette, WebSocket, or
``asyncio`` code, so the caller offloads :meth:`Searcher.search` with
``asyncio.to_thread`` and the search runs unchanged inside a worker thread.

The public surface is the contract adopted by ``chess_ai.api.game_ws`` and
``chess_ai.self_play.runner``:

* :class:`SearchLimits` -- per-search depth and wall-clock budget, with
  :meth:`SearchLimits.from_tier` mapping a :class:`chess_ai.config.DifficultyTier`.
* :class:`SearchInfo` -- per-iteration progress streamed to the AI-thinking
  WebSocket message via the optional ``info_callback``.
* :class:`SearchResult` -- the final result, including the ranked root moves
  used by the self-play annotator's top-3 alternatives.
* :class:`TranspositionTable` -- the Zobrist-keyed transposition table.
* :class:`Searcher` -- the stateful searcher (one per game/connection).
* :func:`find_best_move` -- a transient-searcher convenience wrapper.
* :data:`MATE` -- the mate score magnitude.

The search implements negamax with alpha-beta, iterative deepening with
aspiration windows, principal variation search, a quiescence search with
static-exchange and delta pruning, a transposition table keyed on
:func:`chess.polyglot.zobrist_hash`, null-move pruning, late move reduction,
killer/history move ordering, and futility, check, and singular extensions.
``chess_ai.engine.evaluator`` supplies leaf scores from the side-to-move point
of view, which is the negamax convention used throughout.
"""

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import chess
import chess.polyglot

from chess_ai.config import TT_MAX_ENTRIES, TT_SIZE_MB, DifficultyTier
from chess_ai.engine.book import OpeningBook
from chess_ai.engine.endgame import EndgameTablebase
from chess_ai.engine.evaluator import Evaluator
from chess_ai.engine.move_order import MoveOrderer
from chess_ai.engine.tables import PIECE_VALUES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score constants
# ---------------------------------------------------------------------------
# Mate score magnitude. A forced mate delivered ``n`` plies from the current
# node scores ``MATE - n`` for the winning side and ``-(MATE - n)`` for the
# losing side, so shorter mates score higher.
MATE: int = 30000

# Scores whose magnitude is at least this band are treated as mate scores for
# the transposition-table ply adjustment and for pruning guards.
MATE_THRESHOLD: int = MATE - 1000

# Sentinel bound that exceeds every achievable score; used as +/- infinity for
# the initial alpha-beta window and for fully opened aspiration windows.
INFINITY: int = MATE + 1

# Hard recursion ceiling for the quiescence search to bound check sequences.
MAX_PLY: int = 64


# ---------------------------------------------------------------------------
# Transposition-table bound flags
# ---------------------------------------------------------------------------
# The stored score is exact (a principal-variation node).
TT_EXACT: int = 0
# The stored score is a lower bound (a beta cutoff / fail-high node).
TT_LOWER: int = 1
# The stored score is an upper bound (a fail-low node).
TT_UPPER: int = 2

# Approximate worst-case byte footprint of one stored transposition record in
# CPython (a slotted ``_TTEntry`` plus its reference slot in the bucket array).
# The table derives its bucket count from this so ``num_buckets`` honors BOTH the
# entry cap and the memory budget: ``TT_SIZE_MB`` (256) * 1 MiB / 256 B is
# exactly ``TT_MAX_ENTRIES`` (2**20), so the configured caps agree by design.
_TT_BYTES_PER_ENTRY: int = 256


# ---------------------------------------------------------------------------
# Pruning and timing parameters
# ---------------------------------------------------------------------------
# Futility margins in centipawns indexed by remaining depth (1, 2, 3).
_FUTILITY_MARGIN: dict[int, int] = {1: 200, 2: 350, 3: 500}

# Delta-pruning safety margin in centipawns for the quiescence search.
_DELTA_MARGIN: int = 200

# The wall-clock deadline is polled once every ``_TIME_CHECK_MASK + 1`` nodes.
_TIME_CHECK_MASK: int = 2047

# Move index (1-based) at and beyond which late move reduction may apply.
_LMR_MIN_MOVE_INDEX: int = 4

# Minimum remaining depth at which late move reduction and null-move pruning
# may apply.
_LMR_MIN_DEPTH: int = 3
_NULL_MIN_DEPTH: int = 3

# Singular-extension gating: only attempt it at this remaining depth or deeper.
_SINGULAR_MIN_DEPTH: int = 6
# Centipawn margin below the transposition score used for the singular probe.
_SINGULAR_MARGIN: int = 64

# Default seed for the opening-book RNG so book selection is reproducible.
_DEFAULT_BOOK_SEED: int = 0xC0FFEE


def _zkey(board: chess.Board) -> int:
    """Return the 64-bit Polyglot Zobrist integer key for ``board``.

    This is the single key used for every transposition-table store and probe.
    """
    return chess.polyglot.zobrist_hash(board)


def _score_to_tt(score: int, ply: int) -> int:
    """Convert a node-relative score to a ply-independent transposition score.

    Mate scores encode a distance measured from the search root; the table is
    shared across paths that reach a position at different plies, so a winning
    mate score gains ``ply`` and a losing mate score loses ``ply`` on the way
    in. Non-mate scores are stored unchanged.
    """
    if score >= MATE_THRESHOLD:
        return score + ply
    if score <= -MATE_THRESHOLD:
        return score - ply
    return score


def _score_from_tt(score: int, ply: int) -> int:
    """Convert a stored transposition score back to a node-relative score.

    This reverses :func:`_score_to_tt`: a winning mate score loses ``ply`` and a
    losing mate score gains ``ply`` on the way out, so the returned distance is
    measured from the probing node.
    """
    if score >= MATE_THRESHOLD:
        return score - ply
    if score <= -MATE_THRESHOLD:
        return score + ply
    return score


class _SearchAborted(Exception):
    """Internal signal raised when the wall-clock deadline is reached.

    It unwinds the in-flight depth so the iterative-deepening loop can fall back
    to the result of the last fully completed depth. It never escapes
    :meth:`Searcher.search`.
    """


@dataclass(frozen=True)
class SearchLimits:
    """Depth and wall-clock budget for a single search.

    Attributes:
        depth: Maximum iterative-deepening depth in plies.
        time_budget_s: Wall-clock budget in seconds; the search stops starting
            new depths once it is exhausted.
    """

    depth: int
    time_budget_s: float

    @classmethod
    def from_tier(cls, tier: DifficultyTier) -> "SearchLimits":
        """Build limits from a :class:`chess_ai.config.DifficultyTier`.

        Args:
            tier: The difficulty tier whose ``depth`` and ``time_budget_s`` are
                copied (Easy 4/3.0s, Medium 6/8.0s, Hard 8/15.0s as configured
                in ``chess_ai.config.DIFFICULTY_TIERS``).

        Returns:
            A :class:`SearchLimits` carrying the tier's depth and time budget.
        """
        return cls(depth=tier.depth, time_budget_s=tier.time_budget_s)


@dataclass
class SearchInfo:
    """Per-iteration search progress for streaming AI-thinking updates.

    Attributes:
        depth: The iterative-deepening depth just completed.
        score_cp: The score in centipawns from the side-to-move point of view.
        pv: The principal variation as a list of moves.
        nodes: Total nodes visited so far in this search.
        time_s: Elapsed wall-clock seconds since the search began.
        seldepth: The deepest ply reached, including quiescence.
    """

    depth: int
    score_cp: int
    pv: list[chess.Move]
    nodes: int
    time_s: float
    seldepth: int = 0


@dataclass
class SearchResult:
    """The final result of a search.

    Attributes:
        best_move: The chosen move, or ``None`` only for a terminal position.
        score_cp: The score in centipawns from the side-to-move point of view.
        depth: The deepest fully completed iterative-deepening depth.
        pv: The principal variation as a list of moves.
        nodes: Total nodes visited during the search.
        time_s: Total wall-clock seconds the search took.
        ranked_moves: Root moves with their scores, sorted by score descending,
            so the annotator can present the top alternatives.
        from_book: ``True`` when the move came from the opening book.
        from_tablebase: ``True`` when the move came from the endgame tablebase.
    """

    best_move: chess.Move | None
    score_cp: int
    depth: int
    pv: list[chess.Move] = field(default_factory=list)
    nodes: int = 0
    time_s: float = 0.0
    ranked_moves: list[tuple[chess.Move, int]] = field(default_factory=list)
    from_book: bool = False
    from_tablebase: bool = False


@dataclass(slots=True)
class _TTEntry:
    """A single transposition-table record.

    Declared with ``slots=True`` so each record carries no per-instance
    ``__dict__``; this keeps the per-entry footprint small enough that the
    fixed-size :class:`TranspositionTable` stays within its ``TT_SIZE_MB`` budget.

    Attributes:
        key: The full 64-bit Zobrist key the record was stored under.
        depth: The remaining search depth the score was computed at.
        score: The stored, ply-independent score (mate scores are adjusted).
        flag: One of :data:`TT_EXACT`, :data:`TT_LOWER`, :data:`TT_UPPER`.
        move: The best move found at the node, used first when re-searching.
    """

    key: int
    depth: int
    score: int
    flag: int
    move: chess.Move | None


class TranspositionTable:
    """Zobrist-keyed transposition table with a fixed-size, memory-bounded store.

    Records live in a FIXED-LENGTH bucket array indexed by the low bits of the
    64-bit Polyglot Zobrist key (``key & (num_buckets - 1)``). The bucket count
    is the largest power of two that fits BOTH the entry cap (``max_entries``)
    and the memory budget (``size_mb`` at :data:`_TT_BYTES_PER_ENTRY` bytes per
    record), so the footprint is bounded by construction -- with the configured
    defaults (``2**20`` entries, 256 MB) that is exactly ``2**20`` buckets.

    Replacement is depth-preferred and per-bucket: an incoming record overwrites
    the bucket unless the resident record is a STRICTLY deeper, non-exact entry
    (which is kept). This rule covers both a same-key refresh and a different-key
    collision, so the table never grows past its bucket count and never performs
    the whole-table clear the previous dict-backed implementation used at
    capacity. Because buckets collide, :meth:`probe` verifies an exact key match
    before returning a record. Mate scores are made ply-independent on store and
    node-relative on probe.
    """

    def __init__(self, max_entries: int = TT_MAX_ENTRIES, size_mb: int = TT_SIZE_MB) -> None:
        """Create an empty, fixed-size table sized to the entry and memory caps.

        The bucket count honors both caps and is rounded DOWN to a power of two
        so the index mask (``key & mask``) is exact; with the defaults this is
        ``2**20`` buckets (an 8 MB pointer array) holding up to ``2**20`` records
        within the 256 MB budget.

        Args:
            max_entries: Hard ceiling on stored records (defaults to the
                ``chess_ai.config`` cap of ``2**20``).
            size_mb: Memory budget in megabytes (defaults to the
                ``chess_ai.config`` value of 256). Enforced: the bucket count
                never exceeds ``size_mb * 1 MiB / _TT_BYTES_PER_ENTRY``.
        """
        self.max_entries: int = max_entries
        self.size_mb: int = size_mb
        # Honor BOTH the entry cap and the byte budget, then round down to a
        # power of two so the index mask is exact and overflow is impossible.
        entries_by_memory = max(1, (size_mb * 1024 * 1024) // _TT_BYTES_PER_ENTRY)
        budget = max(1, min(max_entries, entries_by_memory))
        self.num_buckets: int = 1 << (budget.bit_length() - 1)
        self._mask: int = self.num_buckets - 1
        self._buckets: list[_TTEntry | None] = [None] * self.num_buckets
        self._count: int = 0

    def store(
        self,
        key: int,
        depth: int,
        score: int,
        flag: int,
        move: chess.Move | None,
        *,
        ply: int = 0,
    ) -> None:
        """Insert or replace the record in ``key``'s bucket (depth-preferred).

        The resident record is kept only when it is a strictly deeper, non-exact
        entry; otherwise the incoming record replaces it. This evicts shallower
        records on collision deterministically and never exceeds the bucket count.

        Args:
            key: The 64-bit Zobrist key of the position.
            depth: Remaining search depth the score was computed at.
            score: Node-relative score; mate scores are converted to a
                ply-independent form before storage.
            flag: The bound type (:data:`TT_EXACT`, :data:`TT_LOWER`, or
                :data:`TT_UPPER`).
            move: The best move at the node, or ``None``.
            ply: Distance from the search root, used for the mate-score
                adjustment.
        """
        index = key & self._mask
        existing = self._buckets[index]
        # Keep a strictly deeper, non-exact resident; otherwise install the
        # incoming record (covers same-key refresh and cross-key collision).
        if existing is not None and flag != TT_EXACT and depth < existing.depth:
            return
        if existing is None:
            self._count += 1
        self._buckets[index] = _TTEntry(key, depth, _score_to_tt(score, ply), flag, move)

    def probe(self, key: int, *, ply: int = 0) -> _TTEntry | None:
        """Return the record for ``key`` with its score adjusted to ``ply``.

        Args:
            key: The 64-bit Zobrist key of the position.
            ply: Distance from the search root, used to convert a stored mate
                score back to a node-relative distance.

        Returns:
            The matching record (a fresh instance when a mate score had to be
            adjusted), or ``None`` when the bucket is empty or holds a different
            position (a key collision).
        """
        entry = self._buckets[key & self._mask]
        # A bucket may hold a different position (collision); require an exact key
        # match before trusting the record.
        if entry is None or entry.key != key:
            return None
        adjusted = _score_from_tt(entry.score, ply)
        if adjusted == entry.score:
            return entry
        return _TTEntry(entry.key, entry.depth, adjusted, entry.flag, entry.move)

    def warm(self) -> None:
        """Reset the table to an empty, ready state.

        Called once by the application lifespan at startup. It re-touches the
        fixed bucket array so the first search pays no first-touch cost.
        """
        self._buckets = [None] * self.num_buckets
        self._count = 0

    def clear(self) -> None:
        """Empty the table, discarding every stored record."""
        self._buckets = [None] * self.num_buckets
        self._count = 0

    def __len__(self) -> int:
        """Return the number of occupied buckets currently holding a record."""
        return self._count


class Searcher:
    """Stateful alpha-beta searcher reused across the moves of one game.

    The evaluator, move orderer, and transposition table persist across calls
    to :meth:`search`, so the evaluation cache, killer/history tables, and
    stored bounds carry over and ordering improves as the game proceeds. The
    application creates one :class:`Searcher` per game or connection. Every
    method is synchronous; the search must be offloaded with
    ``asyncio.to_thread`` by the caller.
    """

    def __init__(
        self,
        *,
        evaluator: Evaluator | None = None,
        move_orderer: MoveOrderer | None = None,
        tt: TranspositionTable | None = None,
        book: OpeningBook | None = None,
        tablebase: EndgameTablebase | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """Build a searcher, constructing default collaborators as needed.

        Args:
            evaluator: Position evaluator; a fresh :class:`Evaluator` is created
                when omitted.
            move_orderer: Move orderer; a fresh :class:`MoveOrderer` is created
                when omitted.
            tt: Transposition table; a fresh :class:`TranspositionTable` is
                created when omitted.
            book: Optional opening book probed before every search.
            tablebase: Optional endgame tablebase probed for small positions.
            rng: Optional seeded RNG for deterministic book selection; a
                fixed-seed :class:`random.Random` is created when omitted.
        """
        self.evaluator: Evaluator = evaluator if evaluator is not None else Evaluator()
        self.move_orderer: MoveOrderer = move_orderer if move_orderer is not None else MoveOrderer()
        self.tt: TranspositionTable = tt if tt is not None else TranspositionTable()
        self.book: OpeningBook | None = book
        self.tablebase: EndgameTablebase | None = tablebase
        self._rng: random.Random = rng if rng is not None else random.Random(_DEFAULT_BOOK_SEED)

        self._nodes: int = 0
        self._seldepth: int = 0
        self._deadline: float = 0.0
        # Best fully-evaluated root move (and its score) seen during the current
        # search. Tracked so that an abort during the very first
        # iterative-deepening depth can still return a real, evaluated move
        # instead of an arbitrary legal one. Reset at the start of each search.
        self._root_best_move: chess.Move | None = None
        self._root_best_score: int = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def search(
        self,
        board: chess.Board,
        limits: SearchLimits,
        *,
        info_callback: Callable[[SearchInfo], None] | None = None,
    ) -> SearchResult:
        """Search ``board`` within ``limits`` and return the best move.

        The steps run in a fixed order: the opening book is probed first, then
        the endgame tablebase, then an iterative-deepening alpha-beta search
        with aspiration windows. The returned move is always validated against
        ``board.legal_moves`` for non-terminal positions.

        Args:
            board: The position to search. It is restored to its input state
                before returning (every pushed move is popped).
            limits: The depth and wall-clock budget.
            info_callback: Optional callback invoked once per completed depth
                with a :class:`SearchInfo`. It runs on the calling thread and
                should be lightweight.

        Returns:
            A :class:`SearchResult` describing the chosen move, its score, the
            principal variation, the ranked root moves, and the source flags.
        """
        start = time.monotonic()
        self._nodes = 0
        self._seldepth = 0
        self._deadline = start + max(0.0, limits.time_budget_s)
        self._root_best_move = None
        self._root_best_score = 0

        legal_root = list(board.legal_moves)
        if not legal_root:
            terminal = -MATE if board.is_check() else 0
            return SearchResult(
                best_move=None,
                score_cp=terminal,
                depth=0,
                time_s=time.monotonic() - start,
            )

        # Step 1: opening book (Constraint 8 -- probed before any search).
        if self.book is not None:
            book_move = self.book.probe(board, rng=self._rng)
            if book_move is not None and book_move in board.legal_moves:
                return SearchResult(
                    best_move=book_move,
                    score_cp=0,
                    depth=0,
                    pv=[book_move],
                    nodes=0,
                    time_s=time.monotonic() - start,
                    ranked_moves=self._book_ranked_moves(board),
                    from_book=True,
                )

        # Step 2: endgame tablebase.
        if self.tablebase is not None and self.tablebase.should_probe(board):
            tb_move = self._probe_tablebase(board)
            if tb_move is not None and tb_move in board.legal_moves:
                tb_score = self.tablebase.score_cp(board)
                score_cp = tb_score if tb_score is not None else 0
                return SearchResult(
                    best_move=tb_move,
                    score_cp=score_cp,
                    depth=0,
                    pv=[tb_move],
                    nodes=0,
                    time_s=time.monotonic() - start,
                    ranked_moves=[(tb_move, score_cp)],
                    from_tablebase=True,
                )

        # Steps 3-5: iterative deepening with aspiration windows.
        return self._iterative_deepening(board, limits, legal_root, start, info_callback)

    def _iterative_deepening(
        self,
        board: chess.Board,
        limits: SearchLimits,
        legal_root: list[chess.Move],
        start: float,
        info_callback: Callable[[SearchInfo], None] | None,
    ) -> SearchResult:
        """Run the iterative-deepening loop and assemble the final result."""
        max_depth = max(1, limits.depth)
        best_move: chess.Move | None = legal_root[0]
        best_score = 0
        completed_depth = 0
        root_scores: dict[chess.Move, int] = {}
        prev_score = 0

        for depth in range(1, max_depth + 1):
            # Enforce the wall-clock budget before starting each depth,
            # including the first. With no completed depth there is no full
            # result to keep, so we stop here and fall back below to the best
            # root move evaluated so far during the aborted iteration.
            if time.monotonic() >= self._deadline:
                break
            try:
                move, score, scores = self._search_root_window(board, depth, prev_score)
            except _SearchAborted:
                break

            best_move = move if move is not None else best_move
            best_score = score
            root_scores = scores
            prev_score = score
            completed_depth = depth

            self.tt.store(_zkey(board), depth, score, TT_EXACT, best_move, ply=0)
            pv = self._extract_pv(board, best_move, depth)

            if info_callback is not None:
                info_callback(
                    SearchInfo(
                        depth=depth,
                        score_cp=score,
                        pv=list(pv),
                        nodes=self._nodes,
                        time_s=time.monotonic() - start,
                        seldepth=self._seldepth,
                    )
                )

            if abs(score) >= MATE_THRESHOLD:
                break
            if time.monotonic() >= self._deadline:
                break

        # If the very first depth was aborted before completing, fall back to
        # the best root move that was fully evaluated during that aborted
        # iteration (tracked in ``_root_best_move``) rather than to an arbitrary
        # legal move. Later aborts keep the last completed depth's result.
        if completed_depth == 0 and self._root_best_move is not None:
            best_move = self._root_best_move
            best_score = self._root_best_score
            root_scores.setdefault(best_move, best_score)

        if best_move is None or best_move not in board.legal_moves:
            best_move = legal_root[0]
            root_scores.setdefault(best_move, best_score)

        pv = self._extract_pv(board, best_move, max(completed_depth, 1))
        ranked = self._rank_root_moves(root_scores, best_move, best_score)
        logger.debug(
            "search complete: depth=%d score=%d nodes=%d move=%s",
            completed_depth,
            best_score,
            self._nodes,
            best_move.uci() if best_move is not None else "none",
        )
        return SearchResult(
            best_move=best_move,
            score_cp=best_score,
            depth=completed_depth,
            pv=pv,
            nodes=self._nodes,
            time_s=time.monotonic() - start,
            ranked_moves=ranked,
        )

    def _search_root_window(
        self, board: chess.Board, depth: int, prev_score: int
    ) -> tuple[chess.Move | None, int, dict[chess.Move, int]]:
        """Search the root at ``depth`` using an aspiration window for depth >= 2.

        The window starts at ``prev_score +/- 25`` centipawns (the configured
        aspiration delta). A fail-low or fail-high widens the offending side to
        100 and then to infinity and re-searches at the same depth.
        """
        if depth < 2:
            return self._search_root(board, depth, -INFINITY, INFINITY)

        alpha = prev_score - 25
        beta = prev_score + 25
        low_widen = 25
        high_widen = 25
        while True:
            move, score, scores = self._search_root(board, depth, alpha, beta)
            if score <= alpha:
                low_widen = 100 if low_widen == 25 else INFINITY
                alpha = prev_score - low_widen if low_widen != INFINITY else -INFINITY
            elif score >= beta:
                high_widen = 100 if high_widen == 25 else INFINITY
                beta = prev_score + high_widen if high_widen != INFINITY else INFINITY
            else:
                return move, score, scores

    def _search_root(
        self, board: chess.Board, depth: int, alpha: int, beta: int
    ) -> tuple[chess.Move | None, int, dict[chess.Move, int]]:
        """Search every legal root move once and return the best plus all scores.

        Uses principal variation search: the first move takes the full window,
        later moves take a null window and are re-searched on the full window
        when they fall inside ``(alpha, beta)``. Each root move's returned score
        is recorded so the caller can rank the alternatives.
        """
        tt_move = self._tt_move(board)
        ordered = self.move_orderer.order_moves(board, list(board.legal_moves), tt_move, 0)

        best_move: chess.Move | None = None
        best_score = -INFINITY
        scores: dict[chess.Move, int] = {}
        first = True

        for move in ordered:
            extension = 1 if board.gives_check(move) else 0
            board.push(move)
            try:
                if first:
                    score = -self._negamax(board, depth - 1 + extension, -beta, -alpha, 1)
                else:
                    score = -self._negamax(board, depth - 1 + extension, -alpha - 1, -alpha, 1)
                    if alpha < score < beta:
                        score = -self._negamax(board, depth - 1 + extension, -beta, -alpha, 1)
            finally:
                board.pop()

            scores[move] = score
            if score > best_score:
                best_score = score
                best_move = move
                # Record the best fully-evaluated root move so an abort during
                # the first iterative-deepening depth still yields a real move.
                self._root_best_move = move
                self._root_best_score = score
            if score > alpha:
                alpha = score
            first = False
            if alpha >= beta:
                break

        return best_move, best_score, scores

    # ------------------------------------------------------------------
    # Negamax core
    # ------------------------------------------------------------------
    def _negamax(
        self,
        board: chess.Board,
        depth: int,
        alpha: int,
        beta: int,
        ply: int,
        *,
        allow_null: bool = True,
    ) -> int:
        """Return the negamax value of ``board`` in centipawns from its mover's POV.

        Args:
            board: The position to search; restored before returning.
            depth: Remaining search depth in plies (quiescence runs at <= 0).
            alpha: Lower bound of the search window.
            beta: Upper bound of the search window.
            ply: Distance from the search root.
            allow_null: Whether a null-move reduction may be attempted here.

        Returns:
            The score of the position from the side-to-move point of view.

        Raises:
            _SearchAborted: When the wall-clock deadline is reached.
        """
        self._nodes += 1
        if ply > self._seldepth:
            self._seldepth = ply

        if (self._nodes & _TIME_CHECK_MASK) == 0:
            if time.monotonic() >= self._deadline:
                raise _SearchAborted

        if ply > 0:
            if board.is_insufficient_material():
                return 0
            if board.halfmove_clock >= 4 and board.is_repetition(2):
                return 0
            if board.is_fifty_moves():
                return 0

        in_check = board.is_check()
        if depth <= 0:
            if in_check:
                depth = 1
            else:
                return self._quiescence(board, alpha, beta, ply)

        alpha_orig = alpha
        key = _zkey(board)
        tt_entry = self.tt.probe(key, ply=ply)
        tt_move: chess.Move | None = None
        if tt_entry is not None:
            tt_move = tt_entry.move
            if tt_entry.depth >= depth:
                if tt_entry.flag == TT_EXACT:
                    return tt_entry.score
                if tt_entry.flag == TT_LOWER and tt_entry.score > alpha:
                    alpha = tt_entry.score
                elif tt_entry.flag == TT_UPPER and tt_entry.score < beta:
                    beta = tt_entry.score
                if alpha >= beta:
                    return tt_entry.score

        # Null-move pruning: skip a turn and verify the position still fails high.
        if (
            allow_null
            and not in_check
            and depth >= _NULL_MIN_DEPTH
            and beta < MATE_THRESHOLD
            and self._has_non_pawn_material(board)
        ):
            reduction = 3 + depth // 6
            board.push(chess.Move.null())
            try:
                null_score = -self._negamax(
                    board, depth - 1 - reduction, -beta, -beta + 1, ply + 1, allow_null=False
                )
            finally:
                board.pop()
            if null_score >= beta:
                return beta

        # Futility pruning preparation at frontier nodes.
        do_futility = False
        if not in_check and depth in _FUTILITY_MARGIN and -MATE_THRESHOLD < alpha < MATE_THRESHOLD:
            static_eval = self.evaluator.evaluate(board)
            if static_eval + _FUTILITY_MARGIN[depth] <= alpha:
                do_futility = True

        ordered = self.move_orderer.order_moves(board, list(board.legal_moves), tt_move, ply)
        if not ordered:
            return -MATE + ply if in_check else 0

        best_score = -INFINITY
        best_move: chess.Move | None = None
        move_index = 0
        for move in ordered:
            move_index += 1
            is_capture = board.is_capture(move)
            is_promotion = move.promotion is not None
            gives_check = board.gives_check(move)
            is_quiet = not is_capture and not is_promotion and not gives_check

            if do_futility and is_quiet and move_index > 1:
                continue

            extension = 0
            if gives_check:
                extension = 1
            elif (
                depth >= _SINGULAR_MIN_DEPTH
                and tt_move is not None
                and move == tt_move
                and tt_entry is not None
                and tt_entry.depth >= depth - 3
                and tt_entry.flag in (TT_LOWER, TT_EXACT)
                and abs(tt_entry.score) < MATE_THRESHOLD
                and self._is_singular(board, ordered, move, depth, tt_entry.score, ply)
            ):
                extension = 1

            new_depth = depth - 1 + extension

            board.push(move)
            try:
                if move_index == 1:
                    score = -self._negamax(board, new_depth, -beta, -alpha, ply + 1)
                else:
                    reduction = 0
                    if depth >= _LMR_MIN_DEPTH and move_index >= _LMR_MIN_MOVE_INDEX and is_quiet:
                        reduction = max(1, math.floor(math.log(depth) * math.log(move_index) / 2.0))
                        reduction = min(reduction, new_depth - 1)
                        if reduction < 0:
                            reduction = 0
                    score = -self._negamax(
                        board, new_depth - reduction, -alpha - 1, -alpha, ply + 1
                    )
                    if reduction > 0 and score > alpha:
                        score = -self._negamax(board, new_depth, -alpha - 1, -alpha, ply + 1)
                    if alpha < score < beta:
                        score = -self._negamax(board, new_depth, -beta, -alpha, ply + 1)
            finally:
                board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            if score > alpha:
                alpha = score
            if alpha >= beta:
                self.move_orderer.record_cutoff(board, move, depth, ply)
                self.tt.store(key, depth, best_score, TT_LOWER, best_move, ply=ply)
                return best_score

        flag = TT_EXACT if best_score > alpha_orig else TT_UPPER
        self.tt.store(key, depth, best_score, flag, best_move, ply=ply)
        return best_score

    def _is_singular(
        self,
        board: chess.Board,
        ordered: list[chess.Move],
        excluded: chess.Move,
        depth: int,
        tt_score: int,
        ply: int,
    ) -> bool:
        """Report whether ``excluded`` is singularly best among the root's siblings.

        Every move other than ``excluded`` is searched at reduced depth against
        a window just below ``tt_score``. When they all fail low the excluded
        move stands alone and earns a one-ply extension. The search is bounded
        to the reduced depth so it cannot explode.
        """
        s_beta = tt_score - _SINGULAR_MARGIN
        s_depth = (depth - 1) // 2
        for move in ordered:
            if move == excluded:
                continue
            board.push(move)
            try:
                score = -self._negamax(
                    board, s_depth, -s_beta, -s_beta + 1, ply + 1, allow_null=False
                )
            finally:
                board.pop()
            if score >= s_beta:
                return False
        return True

    def _has_non_pawn_material(self, board: chess.Board) -> bool:
        """Report whether the side to move has a non-pawn, non-king piece.

        Used to guard null-move pruning against zugzwang in pawn endings.
        """
        color = board.turn
        non_pawn = board.occupied_co[color] & ~board.pawns & ~board.kings
        return chess.popcount(non_pawn) > 0

    # ------------------------------------------------------------------
    # Quiescence search
    # ------------------------------------------------------------------
    def _quiescence(self, board: chess.Board, alpha: int, beta: int, ply: int) -> int:
        """Return the quiescent value of ``board`` from its mover's POV.

        At a quiet horizon the static evaluation is the stand-pat lower bound;
        only captures and queen promotions are explored, pruned by delta and
        static-exchange margins. When the side to move is in check every legal
        evasion is searched instead, so a checkmate at the horizon is scored
        correctly rather than masked by the stand-pat value.

        Args:
            board: The position to resolve; restored before returning.
            alpha: Lower bound of the search window.
            beta: Upper bound of the search window.
            ply: Distance from the search root.

        Returns:
            The quiescent score from the side-to-move point of view.

        Raises:
            _SearchAborted: When the wall-clock deadline is reached.
        """
        self._nodes += 1
        if ply > self._seldepth:
            self._seldepth = ply

        if (self._nodes & _TIME_CHECK_MASK) == 0:
            if time.monotonic() >= self._deadline:
                raise _SearchAborted

        if board.is_insufficient_material():
            return 0

        if ply >= MAX_PLY:
            return self.evaluator.evaluate(board)

        if board.is_check():
            return self._quiescence_in_check(board, alpha, beta, ply)

        stand_pat = self.evaluator.evaluate(board)
        if stand_pat >= beta:
            return beta
        if stand_pat > alpha:
            alpha = stand_pat

        tactical = [
            move
            for move in board.legal_moves
            if board.is_capture(move) or move.promotion == chess.QUEEN
        ]
        ordered = self.move_orderer.order_moves(board, tactical, None, ply)

        for move in ordered:
            if board.is_capture(move) and move.promotion is None:
                captured_value = self._captured_value(board, move)
                if stand_pat + captured_value + _DELTA_MARGIN < alpha:
                    continue
            if board.is_capture(move) and self.move_orderer.see(board, move) < 0:
                if not board.gives_check(move):
                    continue
            board.push(move)
            try:
                score = -self._quiescence(board, -beta, -alpha, ply + 1)
            finally:
                board.pop()
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score

        return alpha

    def _quiescence_in_check(self, board: chess.Board, alpha: int, beta: int, ply: int) -> int:
        """Resolve an in-check node by searching every legal evasion.

        Returns ``-MATE + ply`` when there are no legal moves (checkmate),
        otherwise the best evasion's score under the usual alpha-beta updates.
        """
        ordered = self.move_orderer.order_moves(board, list(board.legal_moves), None, ply)
        if not ordered:
            return -MATE + ply

        best_score = -INFINITY
        for move in ordered:
            board.push(move)
            try:
                score = -self._quiescence(board, -beta, -alpha, ply + 1)
            finally:
                board.pop()
            if score > best_score:
                best_score = score
            if score > alpha:
                alpha = score
            if alpha >= beta:
                return beta
        return best_score

    def _captured_value(self, board: chess.Board, move: chess.Move) -> int:
        """Return the centipawn value of the piece captured by ``move``.

        En passant captures a pawn; a non-capture contributes zero. Values come
        from :data:`chess_ai.engine.tables.PIECE_VALUES`.
        """
        if board.is_en_passant(move):
            return PIECE_VALUES[chess.PAWN]
        victim = board.piece_at(move.to_square)
        return 0 if victim is None else PIECE_VALUES[victim.piece_type]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _tt_move(self, board: chess.Board) -> chess.Move | None:
        """Return the transposition table's best move for ``board`` if present."""
        entry = self.tt.probe(_zkey(board))
        return entry.move if entry is not None else None

    def _extract_pv(
        self, board: chess.Board, first_move: chess.Move | None, max_len: int
    ) -> list[chess.Move]:
        """Build the principal variation by walking transposition best-moves.

        Starting from ``first_move`` it follows each position's stored best move,
        validating legality at every step, stopping at a repeated position to
        avoid cycles, and capping the length at ``max_len``. The board is
        restored before returning.
        """
        pv: list[chess.Move] = []
        if first_move is None:
            return pv

        pushed = 0
        seen: set[int] = set()
        move: chess.Move | None = first_move
        try:
            while move is not None and len(pv) < max_len:
                if move not in board.legal_moves:
                    break
                pv.append(move)
                board.push(move)
                pushed += 1
                key = _zkey(board)
                if key in seen:
                    break
                seen.add(key)
                entry = self.tt.probe(key)
                move = entry.move if entry is not None else None
        finally:
            for _ in range(pushed):
                board.pop()
        return pv

    def _book_ranked_moves(self, board: chess.Board) -> list[tuple[chess.Move, int]]:
        """Return the book moves for ``board`` as ``(move, weight)`` pairs.

        The opening book lists alternatives heaviest first; the weights stand in
        for scores so the annotator can present the top book alternatives.
        """
        if self.book is None:
            return []
        return [(move, int(weight)) for move, weight in self.book.list_moves(board)]

    def _probe_tablebase(self, board: chess.Board) -> chess.Move | None:
        """Return the tablebase's best move for ``board``, or ``None`` on any miss.

        Any probe error degrades gracefully to ``None`` so the caller falls
        through to a normal search.
        """
        if self.tablebase is None:
            return None
        try:
            return self.tablebase.probe_best_move(board)
        except Exception as exc:  # noqa: BLE001 - any probe failure degrades to search
            logger.warning("Tablebase probe failed for FEN %s: %s", board.fen(), exc)
            return None

    @staticmethod
    def _rank_root_moves(
        root_scores: dict[chess.Move, int],
        best_move: chess.Move | None,
        best_score: int,
    ) -> list[tuple[chess.Move, int]]:
        """Return root moves sorted by score descending for the annotator.

        The chosen ``best_move`` is guaranteed present with ``best_score`` so the
        ranked list never omits the move actually played.
        """
        scores = dict(root_scores)
        if best_move is not None:
            scores[best_move] = best_score
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def find_best_move(
    board: chess.Board,
    limits: SearchLimits,
    *,
    book: OpeningBook | None = None,
    tablebase: EndgameTablebase | None = None,
    info_callback: Callable[[SearchInfo], None] | None = None,
) -> SearchResult:
    """Search ``board`` with a transient :class:`Searcher` and return the result.

    This is a stateless convenience wrapper for tests and the self-play runner.
    It constructs a fresh searcher (and therefore a fresh transposition table
    and ordering state) on every call, so it carries nothing over between moves.

    Args:
        board: The position to search.
        limits: The depth and wall-clock budget.
        book: Optional opening book.
        tablebase: Optional endgame tablebase.
        info_callback: Optional per-depth progress callback.

    Returns:
        The :class:`SearchResult` produced by the transient searcher.
    """
    searcher = Searcher(book=book, tablebase=tablebase)
    return searcher.search(board, limits, info_callback=info_callback)


__all__ = [
    "MATE",
    "SearchLimits",
    "SearchInfo",
    "SearchResult",
    "TranspositionTable",
    "Searcher",
    "find_best_move",
]
