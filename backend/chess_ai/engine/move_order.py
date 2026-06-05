"""Move ordering and static exchange evaluation for the search (pure computation).

This module ranks the legal moves of a position so that the alpha-beta search in
``search.py`` examines the most promising moves first. Strong ordering is what
lets alpha-beta, principal variation search, and late move reduction prune the
tree effectively. :class:`MoveOrderer` ranks moves, in descending priority:

#. the hash (transposition-table) move, when the search supplies one;
#. winning and equal captures, separated from losing captures by static exchange
   evaluation (SEE) and ordered within their band by most-valuable-victim /
   least-valuable-attacker (MVV-LVA);
#. queen promotions, ranked just below captures;
#. the two killer moves recorded for the current ply;
#. the remaining quiet moves, ranked by the history heuristic;
#. losing captures (negative SEE), ranked last of all.

Static exchange evaluation
--------------------------
python-chess exposes no SEE routine, so :meth:`MoveOrderer.see` implements the
classic swap-off algorithm here. It plays out the capture sequence on the target
square -- each side recapturing with its least valuable attacker -- and minimaxes
the resulting gain list. Attacker discovery is x-ray aware: it calls
:meth:`chess.Board.attackers_mask` with an explicitly shrinking occupancy mask so
that a slider lined up behind a piece that has just left the square is revealed.
:meth:`see` never mutates the board it is given; it operates purely on integer
occupancy masks. The king is given a large value (from
:data:`chess_ai.engine.tables.SEE_PIECE_VALUES`) so a sequence never "wins" it,
which also makes the minimax decline a king recapture into a still-defended
square.

Killers and history
-------------------
Killer moves are indexed by ``ply`` (distance from the search root), holding up
to two quiet moves per ply that caused a beta cutoff. The history table is keyed
on ``(side_to_move, from_square, to_square)`` and accumulates ``depth * depth``
credit whenever a quiet move causes a cutoff. :meth:`clear` resets both, which
the search does between games and the tests rely on for deterministic ordering.

Purity
------
This is a pure-computation engine module. It imports only ``chess``, the Python
standard library (``collections``), and :mod:`chess_ai.engine.tables`. It imports
no FastAPI, Starlette, WebSocket, or asyncio code and exposes only plain ``def``
callables, so it is safe to call from inside a worker thread and to unit-test in
isolation. Piece values come solely from :mod:`chess_ai.engine.tables`.
"""

from collections import defaultdict
from collections.abc import Iterable

import chess

from chess_ai.engine.tables import PIECE_VALUES, SEE_PIECE_VALUES

# ---------------------------------------------------------------------------
# Scoring bands (descending priority)
# ---------------------------------------------------------------------------
# The bands are spaced far enough apart that the small within-band adjustments
# (MVV-LVA spread and the clamped history score) can never let a move in a lower
# band outrank a move in a higher band.
HASH_MOVE_SCORE = 1_000_000
WINNING_CAPTURE_BASE = 100_000
PROMOTION_CAPTURE_BONUS = 5_000
QUEEN_PROMOTION_SCORE = 90_000
UNDERPROMOTION_BASE = 80_000
KILLER_1_SCORE = 9_000
KILLER_2_SCORE = 8_000
HISTORY_SCORE_CAP = 7_000
LOSING_CAPTURE_BASE = -100_000

# Inclusive promotion ranks (a1..h1 is rank 0, a8..h8 is rank 7).
_PROMOTION_RANKS = (0, 7)

# Piece types tried from least to most valuable when selecting a recapturer.
_ATTACKER_ORDER = (
    chess.PAWN,
    chess.KNIGHT,
    chess.BISHOP,
    chess.ROOK,
    chess.QUEEN,
    chess.KING,
)


class MoveOrderer:
    """Ranks legal moves for the search and tracks killer / history cutoff data.

    A single instance is owned by the searcher for the duration of a search and
    reused across the iterative-deepening iterations of one move. The killer and
    history tables persist across plies within a search and are reset by
    :meth:`clear`.
    """

    def __init__(self) -> None:
        # Up to two killer moves per ply, indexed by ply (distance from root).
        self.killers: dict[int, list[chess.Move | None]] = defaultdict(lambda: [None, None])
        # Cutoff credit for quiet moves, keyed on (side_to_move, from, to).
        self.history: dict[tuple[bool, int, int], int] = defaultdict(int)

    def clear(self) -> None:
        """Reset the killer and history tables to their empty state."""
        self.killers.clear()
        self.history.clear()

    # ------------------------------------------------------------------
    # Static exchange evaluation
    # ------------------------------------------------------------------
    def _least_valuable_attacker(
        self, board: chess.Board, square: int, color: bool, occupied: int
    ) -> tuple[int | None, int | None]:
        """Return ``(from_square, piece_type)`` of ``color``'s least valuable attacker.

        Only attackers whose square is still set in ``occupied`` are considered, so
        a piece already spent earlier in a capture sequence is excluded and a
        slider revealed by x-ray is included. Returns ``(None, None)`` when
        ``color`` has no remaining attacker of ``square``.
        """
        attackers = board.attackers_mask(color, square, occupied) & occupied
        if not attackers:
            return None, None
        for piece_type in _ATTACKER_ORDER:
            subset = attackers & board.pieces_mask(piece_type, color)
            if subset:
                return chess.lsb(subset), piece_type
        return None, None

    def see(self, board: chess.Board, move: chess.Move) -> int:
        """Return the centipawn material swing of the capture sequence on ``move``.

        Both sides are assumed to recapture on ``move.to_square`` with their least
        valuable attacker until neither can nor wants to continue. A positive
        result is a winning capture, zero an equal trade, and a negative result a
        losing capture. En passant and promotions are handled, and the king carries
        a large value so the sequence never "wins" it. ``board`` is never modified;
        the exchange is played out entirely on integer occupancy masks.
        """
        target = move.to_square
        mover = board.piece_at(move.from_square)
        if mover is None:
            return 0
        mover_color = mover.color

        # Value of the piece first captured on the target square.
        if board.is_en_passant(move):
            captured_value = SEE_PIECE_VALUES[chess.PAWN]
        else:
            victim = board.piece_at(target)
            captured_value = 0 if victim is None else SEE_PIECE_VALUES[victim.piece_type]

        # Occupancy after the first capture: the mover leaves its origin square.
        occupied = board.occupied & ~chess.BB_SQUARES[move.from_square]
        if board.is_en_passant(move):
            # The captured pawn sits beside the target, not on it.
            captured_pawn_sq = target - 8 if mover_color == chess.WHITE else target + 8
            occupied &= ~chess.BB_SQUARES[captured_pawn_sq]
        # The target square is occupied for the rest of the sequence (by whoever
        # currently stands on it), so set its bit for correct x-ray blocking.
        occupied |= chess.BB_SQUARES[target]

        gain = [captured_value]
        if move.promotion is not None:
            gain[0] += SEE_PIECE_VALUES[move.promotion] - SEE_PIECE_VALUES[chess.PAWN]
            value_on_target = SEE_PIECE_VALUES[move.promotion]
        else:
            value_on_target = SEE_PIECE_VALUES[mover.piece_type]

        side = not mover_color
        depth = 0
        while True:
            from_square, piece_type = self._least_valuable_attacker(board, target, side, occupied)
            if from_square is None:
                break
            depth += 1
            gain.append(value_on_target - gain[depth - 1])
            occupied &= ~chess.BB_SQUARES[from_square]
            if piece_type == chess.PAWN and chess.square_rank(target) in _PROMOTION_RANKS:
                # A recapturing pawn that reaches the back rank promotes to a queen.
                gain[depth] += SEE_PIECE_VALUES[chess.QUEEN] - SEE_PIECE_VALUES[chess.PAWN]
                value_on_target = SEE_PIECE_VALUES[chess.QUEEN]
            else:
                value_on_target = SEE_PIECE_VALUES[piece_type]
            side = not side

        # Minimax the gain list back to the root of the exchange. Each side, in
        # turn, would only continue capturing if doing so does not worsen its swing.
        for i in range(len(gain) - 1, 0, -1):
            gain[i - 1] = -max(-gain[i - 1], gain[i])
        return gain[0]

    def see_ge(self, board: chess.Board, move: chess.Move, threshold: int = 0) -> bool:
        """Return whether the static exchange evaluation of ``move`` is >= ``threshold``.

        The default ``threshold`` of zero answers the "is this capture at least
        equal?" question used by quiescence and futility pruning.
        """
        return self.see(board, move) >= threshold

    # ------------------------------------------------------------------
    # MVV-LVA
    # ------------------------------------------------------------------
    def mvv_lva(self, board: chess.Board, move: chess.Move) -> int:
        """Return the MVV-LVA score for ``move``.

        The score is ``value(victim) * 10 - value(attacker)`` using
        :data:`chess_ai.engine.tables.PIECE_VALUES`, so the most valuable victim
        captured by the least valuable attacker scores highest. En passant is
        treated as a pawn capture; a non-capture contributes a zero victim term.
        """
        if board.is_en_passant(move):
            victim_value = PIECE_VALUES[chess.PAWN]
        else:
            victim = board.piece_at(move.to_square)
            victim_value = 0 if victim is None else PIECE_VALUES[victim.piece_type]
        attacker = board.piece_at(move.from_square)
        attacker_value = 0 if attacker is None else PIECE_VALUES[attacker.piece_type]
        return victim_value * 10 - attacker_value

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------
    def score_move(
        self, board: chess.Board, move: chess.Move, tt_move: chess.Move | None, ply: int
    ) -> int:
        """Return the ordering priority of ``move``; a higher score sorts earlier.

        The score places ``move`` into one of the bands described at module level:
        the hash move first, then winning/equal captures (by MVV-LVA), then queen
        promotions, then this ply's killers, then quiet moves by clamped history,
        and finally losing captures. Scoring is deterministic and does not mutate
        ``board``.
        """
        if tt_move is not None and move == tt_move:
            return HASH_MOVE_SCORE

        if board.is_capture(move):
            score = self.mvv_lva(board, move)
            if self.see_ge(board, move):
                score += WINNING_CAPTURE_BASE
            else:
                score += LOSING_CAPTURE_BASE
            if move.promotion == chess.QUEEN:
                score += PROMOTION_CAPTURE_BONUS
            return score

        if move.promotion is not None:
            if move.promotion == chess.QUEEN:
                return QUEEN_PROMOTION_SCORE
            return UNDERPROMOTION_BASE + SEE_PIECE_VALUES[move.promotion]

        killers = self.killers.get(ply)
        if killers is not None:
            if move == killers[0]:
                return KILLER_1_SCORE
            if move == killers[1]:
                return KILLER_2_SCORE

        history_score = self.history.get((board.turn, move.from_square, move.to_square), 0)
        return min(history_score, HISTORY_SCORE_CAP)

    def order_moves(
        self,
        board: chess.Board,
        moves: Iterable[chess.Move],
        tt_move: chess.Move | None,
        ply: int,
    ) -> list[chess.Move]:
        """Return ``moves`` as a new list sorted by descending ordering priority.

        ``moves`` may be any iterable of legal :class:`chess.Move` (the search
        passes ``list(board.legal_moves)``). The board is not mutated, and moves
        that score equally keep their input order, so the result is deterministic.
        """
        scored = [(self.score_move(board, move, tt_move, ply), move) for move in moves]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    # ------------------------------------------------------------------
    # Cutoff bookkeeping
    # ------------------------------------------------------------------
    def record_cutoff(self, board: chess.Board, move: chess.Move, depth: int, ply: int) -> None:
        """Record a beta cutoff caused by ``move`` for killer and history ordering.

        Only quiet moves (neither captures nor promotions) update the tables, since
        tactical moves are already ordered by static exchange evaluation. ``move``
        becomes the first killer for ``ply`` -- shifting any previous first killer
        into the second slot without duplicating it -- and its history entry, keyed
        on ``(side_to_move, from_square, to_square)``, gains ``depth * depth``
        credit.
        """
        if board.is_capture(move) or move.promotion is not None:
            return
        killers = self.killers[ply]
        if killers[0] != move:
            killers[1] = killers[0]
            killers[0] = move
        self.history[(board.turn, move.from_square, move.to_square)] += depth * depth
