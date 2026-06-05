"""Static evaluation for the chess engine (pure computation, no web/async).

This module implements the hand-built engine's static evaluation function. It
scores a :class:`chess.Board` position in centipawns by combining five terms:
material, tapered piece-square placement, pawn structure, king safety, and
mobility. All tuning constants and lookup tables come from
:mod:`chess_ai.engine.tables`; this module holds the algorithms only.

Sign conventions:

* :meth:`Evaluator.evaluate` returns a scalar in centipawns from the
  side-to-move point of view (the negamax convention used by ``search.py``).
* :meth:`Evaluator.evaluate_components` returns an :class:`EvalComponents`
  breakdown from White's point of view (positive favors White), which
  ``self_play/annotator.py`` renders as WHY commentary.
* Invariant: ``evaluate(board)`` equals ``evaluate_components(board).total``
  when White is to move and its negation when Black is to move.

Board and table conventions:

* python-chess square indices run a1 = 0 .. h8 = 63. The piece-square tables in
  :mod:`chess_ai.engine.tables` are flat length-64 lists in a1..h8 order from
  White's point of view; White pieces are indexed by the square and Black
  pieces by :func:`chess.square_mirror` of the square.
* The material term uses ``PIECE_VALUES`` (White minus Black) plus a bishop-pair
  bonus; the positional term interpolates ``PST_MG`` and ``PST_EG`` by the
  material phase (0..24).

Caching:

* Pawn-structure scores are memoized in a pawn hash table keyed on a pawn-only
  Zobrist hash (:func:`pawn_key`).
* Full evaluations are memoized keyed on :func:`chess.polyglot.zobrist_hash`.
* Both caches are bounded and reset by :meth:`Evaluator.clear_cache`.

This module imports only ``chess`` / ``chess.polyglot``, the standard library
(``dataclasses``), and :mod:`chess_ai.engine.tables`; it contains no FastAPI,
Starlette, WebSocket, or asyncio code, so it is safe to run inside a worker
thread and to unit-test in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

import chess
import chess.polyglot

from chess_ai.engine.tables import (
    BACKWARD_PAWN_PENALTY,
    BISHOP_PAIR_BONUS,
    CONNECTED_PAWN_BONUS,
    DOUBLED_PAWN_PENALTY,
    ISOLATED_PAWN_PENALTY,
    KING_OPEN_FILE_PENALTY,
    KING_SHIELD_BONUS,
    KING_ZONE_ATTACK_WEIGHT,
    MOBILITY_WEIGHT,
    PASSED_PAWN_BONUS,
    PHASE_MAX,
    PHASE_WEIGHTS,
    PIECE_VALUES,
    PST_EG,
    PST_MG,
)

# Polyglot random numbers. The first 768 entries are the piece contributions
# laid out as ``64 * kind_of_piece + square``; ``kind_of_piece`` is 0 for black
# pawns and 1 for white pawns, which is all the pawn-only key uses.
_POLYGLOT_RANDOM_ARRAY = chess.polyglot.POLYGLOT_RANDOM_ARRAY
_WHITE_PAWN_BASE = 64 * 1
_BLACK_PAWN_BASE = 64 * 0

# Files adjacent to each file index 0..7, precomputed once for pawn-structure
# and king-safety lookups.
_ADJACENT_FILES: tuple[tuple[int, ...], ...] = tuple(
    tuple(neighbor for neighbor in (file - 1, file + 1) if 0 <= neighbor <= 7) for file in range(8)
)

# Piece types scored for material and placement, in a fixed iteration order.
# Kings carry zero material value but are scored positionally.
_SCORED_PIECE_TYPES: tuple[int, ...] = (
    chess.PAWN,
    chess.KNIGHT,
    chess.BISHOP,
    chess.ROOK,
    chess.QUEEN,
    chess.KING,
)

# Piece types that contribute to the mobility term.
_MOBILITY_PIECE_TYPES: tuple[int, ...] = (
    chess.KNIGHT,
    chess.BISHOP,
    chess.ROOK,
    chess.QUEEN,
)


def pawn_key(board: chess.Board) -> int:
    """Return a 64-bit Zobrist key derived only from pawn placement.

    The key XORs the Polyglot random number for every pawn, using the Polyglot
    piece index ``64 * kind_of_piece + square`` with ``kind_of_piece`` equal to
    1 for white pawns and 0 for black pawns. It is independent of every non-pawn
    piece, the side to move, castling rights, and en passant state, so it is
    stable across non-pawn moves and changes whenever the pawn skeleton changes.
    It keys the evaluator's pawn hash table.
    """
    key = 0
    for square in board.pieces(chess.PAWN, chess.WHITE):
        key ^= _POLYGLOT_RANDOM_ARRAY[_WHITE_PAWN_BASE + square]
    for square in board.pieces(chess.PAWN, chess.BLACK):
        key ^= _POLYGLOT_RANDOM_ARRAY[_BLACK_PAWN_BASE + square]
    return key


@dataclass(frozen=True)
class EvalComponents:
    """Per-term static evaluation breakdown in centipawns, from White's POV.

    Positive values favor White. ``total`` is the sum of the five scoring terms
    and ``phase`` is the material game phase (0 = endgame .. 24 = midgame) that
    drives the tapered terms. ``self_play/annotator.py`` renders these fields as
    WHY commentary.
    """

    material: int
    positional: int
    pawns: int
    king_safety: int
    mobility: int
    total: int
    phase: int


class Evaluator:
    """Static position evaluator with bounded pawn and evaluation caches.

    A single instance is reused across a search. Identical positions evaluate
    identically (the evaluation contains no randomness). Both caches are bounded
    so they cannot grow without limit during a long game, and
    :meth:`clear_cache` resets them between games or tests.
    """

    # Cache size ceilings (number of entries). On overflow a cache is cleared.
    _PAWN_CACHE_LIMIT: int = 1 << 16
    _EVAL_CACHE_LIMIT: int = 1 << 18

    def __init__(self) -> None:
        self._pawn_cache: dict[int, int] = {}
        self._eval_cache: dict[int, int] = {}

    # ------------------------------------------------------------------ phase
    def phase(self, board: chess.Board) -> int:
        """Return the material game phase clamped to ``[0, PHASE_MAX]``.

        The phase sums ``PHASE_WEIGHTS`` over every non-king, non-pawn piece on
        the board (knight and bishop = 1, rook = 2, queen = 4). The standard
        start position totals ``PHASE_MAX`` (24, midgame); a bare king-and-pawn
        endgame totals 0. Promotions can push the raw sum above ``PHASE_MAX``,
        so the result is clamped.
        """
        raw = 0
        for piece_type, weight in PHASE_WEIGHTS.items():
            if weight:
                raw += weight * (
                    len(board.pieces(piece_type, chess.WHITE))
                    + len(board.pieces(piece_type, chess.BLACK))
                )
        if raw < 0:
            return 0
        if raw > PHASE_MAX:
            return PHASE_MAX
        return raw

    # ----------------------------------------------- material and placement
    def _material_and_positional(self, board: chess.Board, phase: int) -> tuple[int, int]:
        """Return ``(material, positional)`` in centipawns from White's POV.

        Material is the ``PIECE_VALUES`` difference (White minus Black) plus a
        bishop-pair bonus for each side holding two or more bishops. Positional
        is the midgame/endgame piece-square delta interpolated by ``phase``:
        ``(mg * phase + eg * (PHASE_MAX - phase)) // PHASE_MAX``. White pieces
        index the tables by square; Black pieces index by
        :func:`chess.square_mirror` and are subtracted.
        """
        material = 0
        midgame = 0
        endgame = 0
        for piece_type in _SCORED_PIECE_TYPES:
            value = PIECE_VALUES[piece_type]
            table_mg = PST_MG[piece_type]
            table_eg = PST_EG[piece_type]
            for square in board.pieces(piece_type, chess.WHITE):
                material += value
                midgame += table_mg[square]
                endgame += table_eg[square]
            for square in board.pieces(piece_type, chess.BLACK):
                mirrored = chess.square_mirror(square)
                material -= value
                midgame -= table_mg[mirrored]
                endgame -= table_eg[mirrored]

        if len(board.pieces(chess.BISHOP, chess.WHITE)) >= 2:
            material += BISHOP_PAIR_BONUS
        if len(board.pieces(chess.BISHOP, chess.BLACK)) >= 2:
            material -= BISHOP_PAIR_BONUS

        positional = (midgame * phase + endgame * (PHASE_MAX - phase)) // PHASE_MAX
        return material, positional

    # ------------------------------------------------- pawn structure (cached)
    def _eval_pawns(self, board: chess.Board) -> int:
        """Return the pawn-structure score in centipawns from White's POV.

        The score combines doubled, isolated, and backward pawn penalties with
        passed-pawn and connected-pawn bonuses for each side (White minus
        Black). Results are memoized in a pawn hash table keyed on
        :func:`pawn_key`; the cache is cleared when it exceeds its bound.
        """
        key = pawn_key(board)
        cached = self._pawn_cache.get(key)
        if cached is not None:
            return cached

        white_pawns = board.pieces(chess.PAWN, chess.WHITE)
        black_pawns = board.pieces(chess.PAWN, chess.BLACK)
        score = self._pawn_score_for(white_pawns, black_pawns, chess.WHITE) - self._pawn_score_for(
            black_pawns, white_pawns, chess.BLACK
        )

        if len(self._pawn_cache) >= self._PAWN_CACHE_LIMIT:
            self._pawn_cache.clear()
        self._pawn_cache[key] = score
        return score

    def _pawn_score_for(
        self,
        own_pawns: chess.SquareSet,
        enemy_pawns: chess.SquareSet,
        color: chess.Color,
    ) -> int:
        """Return ``color``'s pawn-structure score from its own POV (centipawns).

        Penalties (doubled, isolated, backward) are negative and bonuses
        (passed, connected) are positive, exactly as defined in
        :mod:`chess_ai.engine.tables`.
        """
        if not own_pawns:
            return 0

        own_file_counts = [0] * 8
        own_ranks_by_file: list[list[int]] = [[] for _ in range(8)]
        for square in own_pawns:
            file_index = chess.square_file(square)
            own_file_counts[file_index] += 1
            own_ranks_by_file[file_index].append(chess.square_rank(square))

        enemy_ranks_by_file: list[list[int]] = [[] for _ in range(8)]
        for square in enemy_pawns:
            enemy_ranks_by_file[chess.square_file(square)].append(chess.square_rank(square))

        score = 0

        # Doubled pawns: one penalty per extra pawn sharing a file.
        for count in own_file_counts:
            if count > 1:
                score += DOUBLED_PAWN_PENALTY * (count - 1)

        for square in own_pawns:
            file_index = chess.square_file(square)
            rank_index = chess.square_rank(square)
            adjacent = _ADJACENT_FILES[file_index]
            has_adjacent_pawn = any(own_file_counts[f] for f in adjacent)

            if not has_adjacent_pawn:
                score += ISOLATED_PAWN_PENALTY

            if self._is_passed(file_index, rank_index, color, enemy_ranks_by_file):
                relative_rank = rank_index if color == chess.WHITE else 7 - rank_index
                score += PASSED_PAWN_BONUS[relative_rank]

            if has_adjacent_pawn and self._is_backward(
                file_index, rank_index, color, own_ranks_by_file, enemy_ranks_by_file
            ):
                score += BACKWARD_PAWN_PENALTY

            # Connected (phalanx): a friendly pawn on the next file at the same
            # rank. Only the higher file of each pair is tested to avoid double
            # counting.
            next_file = file_index + 1
            if next_file <= 7 and rank_index in own_ranks_by_file[next_file]:
                score += CONNECTED_PAWN_BONUS

        return score

    @staticmethod
    def _is_passed(
        file_index: int,
        rank_index: int,
        color: chess.Color,
        enemy_ranks_by_file: list[list[int]],
    ) -> bool:
        """Return True if no enemy pawn lies ahead on the same or adjacent file.

        "Ahead" means toward the enemy back rank: higher ranks for White, lower
        ranks for Black.
        """
        for file_to_check in (file_index - 1, file_index, file_index + 1):
            if file_to_check < 0 or file_to_check > 7:
                continue
            for enemy_rank in enemy_ranks_by_file[file_to_check]:
                if color == chess.WHITE:
                    if enemy_rank > rank_index:
                        return False
                elif enemy_rank < rank_index:
                    return False
        return True

    @staticmethod
    def _is_backward(
        file_index: int,
        rank_index: int,
        color: chess.Color,
        own_ranks_by_file: list[list[int]],
        enemy_ranks_by_file: list[list[int]],
    ) -> bool:
        """Return True if the pawn lags its neighbors and cannot advance safely.

        A pawn is backward when every friendly pawn on an adjacent file is
        further advanced than it (so none can support an advance) and the stop
        square in front of it is controlled by an enemy pawn.
        """
        adjacent = _ADJACENT_FILES[file_index]
        for f in adjacent:
            for own_rank in own_ranks_by_file[f]:
                if color == chess.WHITE:
                    if own_rank <= rank_index:
                        return False
                elif own_rank >= rank_index:
                    return False

        # The stop square is attacked by an enemy pawn standing two ranks ahead
        # on an adjacent file.
        if color == chess.WHITE:
            control_rank = rank_index + 2
            if control_rank > 7:
                return False
        else:
            control_rank = rank_index - 2
            if control_rank < 0:
                return False
        return any(control_rank in enemy_ranks_by_file[f] for f in adjacent)

    # ---------------------------------------------------------- king safety
    def _king_safety(self, board: chess.Board, phase: int) -> int:
        """Return the king-safety score in centipawns from White's POV.

        Each king is scored for its pawn shield, open or half-open files beside
        it, and enemy attacks on its king zone; the difference (White minus
        Black) is scaled by ``phase`` so it weighs more in the midgame.
        """
        raw = self._king_safety_for(board, chess.WHITE) - self._king_safety_for(board, chess.BLACK)
        return (raw * phase) // PHASE_MAX

    def _king_safety_for(self, board: chess.Board, color: chess.Color) -> int:
        """Return ``color``'s king-safety score from its own POV (centipawns)."""
        king_square = board.king(color)
        if king_square is None:
            return 0

        king_file = chess.square_file(king_square)
        king_rank = chess.square_rank(king_square)
        own_pawns = board.pieces(chess.PAWN, color)
        own_pawn_files = {chess.square_file(square) for square in own_pawns}
        zone_files = [f for f in (king_file - 1, king_file, king_file + 1) if 0 <= f <= 7]

        score = 0

        # Pawn shield: friendly pawns on the king's file and its neighbors, on
        # the two ranks directly in front of the king.
        if color == chess.WHITE:
            shield_ranks = (king_rank + 1, king_rank + 2)
        else:
            shield_ranks = (king_rank - 1, king_rank - 2)
        for f in zone_files:
            for r in shield_ranks:
                if 0 <= r <= 7 and chess.square(f, r) in own_pawns:
                    score += KING_SHIELD_BONUS

        # Open or half-open files beside the king (no friendly pawn on the file).
        for f in zone_files:
            if f not in own_pawn_files:
                score += KING_OPEN_FILE_PENALTY

        # Enemy attacks on the king zone (the king square plus its neighbors).
        enemy = not color
        zone = chess.SquareSet(board.attacks(king_square))
        zone.add(king_square)
        attack_units = 0
        for square in zone:
            attack_units += len(board.attackers(enemy, square))
        score -= KING_ZONE_ATTACK_WEIGHT * attack_units

        return score

    # -------------------------------------------------------------- mobility
    def _mobility(self, board: chess.Board) -> int:
        """Return the mobility score in centipawns from White's POV.

        For each side the score sums, over knights, bishops, rooks, and queens,
        ``MOBILITY_WEIGHT`` times the number of squares the piece attacks that
        are not occupied by a friendly piece. The result is White minus Black.
        """
        return self._mobility_for(board, chess.WHITE) - self._mobility_for(board, chess.BLACK)

    def _mobility_for(self, board: chess.Board, color: chess.Color) -> int:
        """Return ``color``'s mobility score from its own POV (centipawns)."""
        own_occupied = board.occupied_co[color]
        score = 0
        for piece_type in _MOBILITY_PIECE_TYPES:
            weight = MOBILITY_WEIGHT[piece_type]
            if not weight:
                continue
            for square in board.pieces(piece_type, color):
                attack_mask = int(board.attacks(square))
                free = attack_mask & ~own_occupied & chess.BB_ALL
                score += weight * free.bit_count()
        return score

    # ----------------------------------------------- public evaluation API
    def evaluate_components(self, board: chess.Board) -> EvalComponents:
        """Return the per-term evaluation breakdown from White's point of view.

        ``total`` is the sum of the five terms (positive favors White); it is
        not negated for the side to move. This is the breakdown rendered by the
        self-play annotator.
        """
        phase = self.phase(board)
        material, positional = self._material_and_positional(board, phase)
        pawns = self._eval_pawns(board)
        king_safety = self._king_safety(board, phase)
        mobility = self._mobility(board)
        total = material + positional + pawns + king_safety + mobility
        return EvalComponents(
            material=material,
            positional=positional,
            pawns=pawns,
            king_safety=king_safety,
            mobility=mobility,
            total=total,
            phase=phase,
        )

    def evaluate(self, board: chess.Board) -> int:
        """Return the static evaluation in centipawns from the side-to-move POV.

        The White-point-of-view total is negated when Black is to move so the
        score follows the negamax convention used by ``search.py``. Results are
        memoized keyed on :func:`chess.polyglot.zobrist_hash`; the cache is
        cleared when it exceeds its bound. The method does not raise on terminal
        positions (mate and stalemate scoring is the search's responsibility).
        """
        key = chess.polyglot.zobrist_hash(board)
        cached = self._eval_cache.get(key)
        if cached is not None:
            return cached

        total_white = self.evaluate_components(board).total
        score = total_white if board.turn == chess.WHITE else -total_white

        if len(self._eval_cache) >= self._EVAL_CACHE_LIMIT:
            self._eval_cache.clear()
        self._eval_cache[key] = score
        return score

    def clear_cache(self) -> None:
        """Reset the evaluation and pawn hash caches.

        Used between games and in tests to guarantee deterministic, cache-free
        evaluation.
        """
        self._eval_cache.clear()
        self._pawn_cache.clear()


__all__ = ["EvalComponents", "Evaluator", "pawn_key"]
