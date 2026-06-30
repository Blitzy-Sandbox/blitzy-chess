"""Static evaluation data for the chess engine (pure data, no algorithms).

This module is the single source of the engine's tuning constants and lookup
tables. It is consumed by:

* ``evaluator.py`` -- piece values, piece-square tables, phase weights, and the
  pawn-structure / king-safety / mobility weight constants.
* ``move_order.py`` -- ``PIECE_VALUES`` and ``SEE_PIECE_VALUES`` for MVV-LVA
  ordering and static exchange evaluation.
* ``search.py`` -- the transposition-table sizing constants.

Conventions used throughout this module:

* Square indexing is python-chess native: index ``0`` is a1, ``1`` is b1, ...,
  ``8`` is a2, ..., ``63`` is h8. Every piece-square table is stored as a flat
  ``list[int]`` of length 64 in this a1..h8 order, from White's point of view.
  ``evaluator.py`` indexes White pieces by ``sq`` and Black pieces by
  ``chess.square_mirror(sq)``.
* Piece-square table values are POSITION-ONLY deltas in centipawns; the piece
  material value is held separately in ``PIECE_VALUES_MG`` / ``PIECE_VALUES_EG``.
  The evaluator adds material and positional terms, then interpolates by phase.
* Weight constants are expressed from the owning side's point of view in
  centipawns. Penalties are negative and bonuses are positive; the evaluator
  computes each term for White and Black and combines White minus Black.

This module imports only ``chess`` (for piece-type and square constants and
``chess.square_mirror``) and the standard library; it contains no web-framework,
asyncio, or I/O code so it is safe to import inside a worker thread.
"""

from typing import Final

import chess

# ---------------------------------------------------------------------------
# Piece values (centipawns)
# ---------------------------------------------------------------------------
# General-purpose material values (classic "simplified" set). Used for move
# ordering (MVV-LVA) and search margins. The king contributes 0 to material.
PIECE_VALUES: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# Tapered material values paired with the PeSTO piece-square tables below.
# The evaluator uses these (not PIECE_VALUES) for the phase-interpolated
# material term: midgame values.
PIECE_VALUES_MG: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 82,
    chess.KNIGHT: 337,
    chess.BISHOP: 365,
    chess.ROOK: 477,
    chess.QUEEN: 1025,
    chess.KING: 0,
}

# Endgame material values.
PIECE_VALUES_EG: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 94,
    chess.KNIGHT: 281,
    chess.BISHOP: 297,
    chess.ROOK: 512,
    chess.QUEEN: 936,
    chess.KING: 0,
}

# Values used by static exchange evaluation in move_order.py. The king value is
# large so a capture sequence never "wins" the king.
SEE_PIECE_VALUES: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# ---------------------------------------------------------------------------
# Game-phase weights (material phase, clamped to [0, PHASE_MAX] by the evaluator)
# ---------------------------------------------------------------------------
# Summed over every piece on the board, the standard start position totals 24.
PHASE_WEIGHTS: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 0,
    chess.KNIGHT: 1,
    chess.BISHOP: 1,
    chess.ROOK: 2,
    chess.QUEEN: 4,
    chess.KING: 0,
}

PHASE_MAX: Final[int] = 24


# ---------------------------------------------------------------------------
# Piece-square table construction
# ---------------------------------------------------------------------------
def _orient(rows_a8_to_h1: tuple[tuple[int, ...], ...]) -> list[int]:
    """Flatten an 8x8 table written rank-8-first into python-chess a1..h8 order.

    ``rows_a8_to_h1[0]`` is rank 8 (a8..h8) and ``rows_a8_to_h1[7]`` is rank 1
    (a1..h1), which is how the source PeSTO tables below are transcribed. The
    returned flat list is indexed by the python-chess square index, so
    ``result[chess.E4]`` is the value on e4 from White's point of view.
    """
    flat_a8_to_h1: list[int] = [value for rank in rows_a8_to_h1 for value in rank]
    return [flat_a8_to_h1[chess.square_mirror(square)] for square in range(64)]


# Source PeSTO / Rofchade piece-square tables, transcribed rank-8-first
# (rows[0] = a8..h8, rows[7] = a1..h1). Each is converted once to python-chess
# a1..h8 order by _orient() when building PST_MG / PST_EG below.

# Pawn -- midgame
_MG_PAWN: tuple[tuple[int, ...], ...] = (
    (0, 0, 0, 0, 0, 0, 0, 0),
    (98, 134, 61, 95, 68, 126, 34, -11),
    (-6, 7, 26, 31, 65, 56, 25, -20),
    (-14, 13, 6, 21, 23, 12, 17, -23),
    (-27, -2, -5, 12, 17, 6, 10, -25),
    (-26, -4, -4, -10, 3, 3, 33, -12),
    (-35, -1, -20, -23, -15, 24, 38, -22),
    (0, 0, 0, 0, 0, 0, 0, 0),
)

# Pawn -- endgame
_EG_PAWN: tuple[tuple[int, ...], ...] = (
    (0, 0, 0, 0, 0, 0, 0, 0),
    (178, 173, 158, 134, 147, 132, 165, 187),
    (94, 100, 85, 67, 56, 53, 82, 84),
    (32, 24, 13, 5, -2, 4, 17, 17),
    (13, 9, -3, -7, -7, -8, 3, -1),
    (4, 7, -6, 1, 0, -5, -1, -8),
    (13, 8, 8, 10, 13, 0, 2, -7),
    (0, 0, 0, 0, 0, 0, 0, 0),
)

# Knight -- midgame
_MG_KNIGHT: tuple[tuple[int, ...], ...] = (
    (-167, -89, -34, -49, 61, -97, -15, -107),
    (-73, -41, 72, 36, 23, 62, 7, -17),
    (-47, 60, 37, 65, 84, 129, 73, 44),
    (-9, 17, 19, 53, 37, 69, 18, 22),
    (-13, 4, 16, 13, 28, 19, 21, -8),
    (-23, -9, 12, 10, 19, 17, 25, -16),
    (-29, -53, -12, -3, -1, 18, -14, -19),
    (-105, -21, -58, -33, -17, -28, -19, -23),
)

# Knight -- endgame
_EG_KNIGHT: tuple[tuple[int, ...], ...] = (
    (-58, -38, -13, -28, -31, -27, -63, -99),
    (-25, -8, -25, -2, -9, -25, -24, -52),
    (-24, -20, 10, 9, -1, -9, -19, -41),
    (-17, 3, 22, 22, 22, 11, 8, -18),
    (-18, -6, 16, 25, 16, 17, 4, -18),
    (-23, -3, -1, 15, 10, -3, -20, -22),
    (-42, -20, -10, -5, -2, -20, -23, -44),
    (-29, -51, -23, -15, -22, -18, -50, -64),
)

# Bishop -- midgame
_MG_BISHOP: tuple[tuple[int, ...], ...] = (
    (-29, 4, -82, -37, -25, -42, 7, -8),
    (-26, 16, -18, -13, 30, 59, 18, -47),
    (-16, 37, 43, 40, 35, 50, 37, -2),
    (-4, 5, 19, 50, 37, 37, 7, -2),
    (-6, 13, 13, 26, 34, 12, 10, 4),
    (0, 15, 15, 15, 14, 27, 18, 10),
    (4, 15, 16, 0, 7, 21, 33, 1),
    (-33, -3, -14, -21, -13, -12, -39, -21),
)

# Bishop -- endgame
_EG_BISHOP: tuple[tuple[int, ...], ...] = (
    (-14, -21, -11, -8, -7, -9, -17, -24),
    (-8, -4, 7, -12, -3, -13, -4, -14),
    (2, -8, 0, -1, -2, 6, 0, 4),
    (-3, 9, 12, 9, 14, 10, 3, 2),
    (-6, 3, 13, 19, 7, 10, -3, -9),
    (-12, -3, 8, 10, 13, 3, -7, -15),
    (-14, -18, -7, -1, 4, -9, -15, -27),
    (-23, -9, -23, -5, -9, -16, -5, -17),
)

# Rook -- midgame
_MG_ROOK: tuple[tuple[int, ...], ...] = (
    (32, 42, 32, 51, 63, 9, 31, 43),
    (27, 32, 58, 62, 80, 67, 26, 44),
    (-5, 19, 26, 36, 17, 45, 61, 16),
    (-24, -11, 7, 26, 24, 35, -8, -20),
    (-36, -26, -12, -1, 9, -7, 6, -23),
    (-45, -25, -16, -17, 3, 0, -5, -33),
    (-44, -16, -20, -9, -1, 11, -6, -71),
    (-19, -13, 1, 17, 16, 7, -37, -26),
)

# Rook -- endgame
_EG_ROOK: tuple[tuple[int, ...], ...] = (
    (13, 10, 18, 15, 12, 12, 8, 5),
    (11, 13, 13, 11, -3, 3, 8, 3),
    (7, 7, 7, 5, 4, -3, -5, -3),
    (4, 3, 13, 1, 2, 1, -1, 2),
    (3, 5, 8, 4, -5, -6, -8, -11),
    (-4, 0, -5, -1, -7, -12, -8, -16),
    (-6, -6, 0, 2, -9, -9, -11, -3),
    (-9, 2, 3, -1, -5, -13, 4, -20),
)

# Queen -- midgame
_MG_QUEEN: tuple[tuple[int, ...], ...] = (
    (-28, 0, 29, 12, 59, 44, 43, 45),
    (-24, -39, -5, 1, -16, 57, 28, 54),
    (-13, -17, 7, 8, 29, 56, 47, 57),
    (-27, -27, -16, -16, -1, 17, -2, 1),
    (-9, -26, -9, -10, -2, -4, 3, -3),
    (-14, 2, -11, -2, -5, 2, 14, 5),
    (-35, -8, 11, 2, 8, 15, -3, 1),
    (-1, -18, -9, 10, -15, -25, -31, -50),
)

# Queen -- endgame
_EG_QUEEN: tuple[tuple[int, ...], ...] = (
    (-9, 22, 22, 27, 27, 19, 10, 20),
    (-17, 20, 32, 41, 58, 25, 30, 0),
    (-20, 6, 9, 49, 47, 35, 19, 9),
    (3, 22, 24, 45, 57, 40, 57, 36),
    (-18, 28, 19, 47, 31, 34, 39, 23),
    (-16, -27, 15, 6, 9, 17, 10, 5),
    (-22, -23, -30, -16, -16, -23, -36, -32),
    (-33, -28, -22, -43, -5, -32, -20, -41),
)

# King -- midgame
_MG_KING: tuple[tuple[int, ...], ...] = (
    (-65, 23, 16, -15, -56, -34, 2, 13),
    (29, -1, -20, -7, -8, -4, -38, -29),
    (-9, 24, 2, -16, -20, 6, 22, -22),
    (-17, -20, -12, -27, -30, -25, -14, -36),
    (-49, -1, -27, -39, -46, -44, -33, -51),
    (-14, -14, -22, -46, -44, -30, -15, -27),
    (1, 7, -8, -64, -43, -16, 9, 8),
    (-15, 36, 12, -54, 8, -28, 24, 14),
)

# King -- endgame
_EG_KING: tuple[tuple[int, ...], ...] = (
    (-74, -35, -18, -18, -11, 15, 4, -17),
    (-12, 17, 14, 17, 17, 38, 23, 11),
    (10, 17, 23, 15, 20, 45, 44, 13),
    (-8, 22, 24, 27, 26, 33, 26, 3),
    (-18, -4, 21, 24, 27, 23, 9, -11),
    (-19, -3, 11, 21, 23, 16, 7, -9),
    (-27, -11, 4, 13, 14, 4, -5, -17),
    (-53, -34, -21, -11, -28, -14, -24, -43),
)

# Midgame piece-square tables (centipawn positional deltas, White POV, a1..h8).
PST_MG: Final[dict[chess.PieceType, list[int]]] = {
    chess.PAWN: _orient(_MG_PAWN),
    chess.KNIGHT: _orient(_MG_KNIGHT),
    chess.BISHOP: _orient(_MG_BISHOP),
    chess.ROOK: _orient(_MG_ROOK),
    chess.QUEEN: _orient(_MG_QUEEN),
    chess.KING: _orient(_MG_KING),
}

# Endgame piece-square tables (centipawn positional deltas, White POV, a1..h8).
PST_EG: Final[dict[chess.PieceType, list[int]]] = {
    chess.PAWN: _orient(_EG_PAWN),
    chess.KNIGHT: _orient(_EG_KNIGHT),
    chess.BISHOP: _orient(_EG_BISHOP),
    chess.ROOK: _orient(_EG_ROOK),
    chess.QUEEN: _orient(_EG_QUEEN),
    chess.KING: _orient(_EG_KING),
}


# ---------------------------------------------------------------------------
# Evaluation weight constants (centipawns, owning-side point of view)
# ---------------------------------------------------------------------------
# Pawn structure. Penalties are negative; bonuses are positive.
DOUBLED_PAWN_PENALTY: Final[int] = -10
ISOLATED_PAWN_PENALTY: Final[int] = -15
BACKWARD_PAWN_PENALTY: Final[int] = -8

# Passed-pawn bonus indexed by the pawn's own rank index (0 = own first rank,
# ..., 7 = promotion rank). The evaluator uses chess.square_rank(sq) for White
# and 7 - chess.square_rank(sq) for Black.
PASSED_PAWN_BONUS: Final[tuple[int, ...]] = (0, 10, 17, 25, 45, 75, 130, 0)

# Bonus for two pawns of the same color defending adjacent files (phalanx).
CONNECTED_PAWN_BONUS: Final[int] = 8

# Bonus for holding both bishops.
BISHOP_PAIR_BONUS: Final[int] = 35

# King safety. KING_SHIELD_BONUS rewards each friendly pawn shielding the king;
# KING_OPEN_FILE_PENALTY penalizes an open or half-open file beside the king.
KING_SHIELD_BONUS: Final[int] = 12
KING_OPEN_FILE_PENALTY: Final[int] = -25

# Centipawns of king danger per attacking-piece weight unit in the king zone.
# The evaluator subtracts accumulated danger from the defended side's score.
KING_ZONE_ATTACK_WEIGHT: Final[int] = 10

# Mobility weight in centipawns per pseudo-legal move, per piece type.
MOBILITY_WEIGHT: Final[dict[chess.PieceType, int]] = {
    chess.PAWN: 0,
    chess.KNIGHT: 4,
    chess.BISHOP: 4,
    chess.ROOK: 2,
    chess.QUEEN: 1,
    chess.KING: 0,
}

# ---------------------------------------------------------------------------
# Transposition-table sizing (consumed by search.py)
# ---------------------------------------------------------------------------
# These mirror chess_ai.config, which documents the engine as the canonical
# owner; the values are kept identical (256 MB / 2**20 entries).
TT_SIZE_MB: Final[int] = 256
TT_MAX_ENTRIES: Final[int] = 2**20
# Approximate per-entry footprint in bytes (documentation only).
TT_ENTRY_BYTES: Final[int] = 32


__all__ = [
    "PIECE_VALUES",
    "PIECE_VALUES_MG",
    "PIECE_VALUES_EG",
    "SEE_PIECE_VALUES",
    "PST_MG",
    "PST_EG",
    "PHASE_WEIGHTS",
    "PHASE_MAX",
    "DOUBLED_PAWN_PENALTY",
    "ISOLATED_PAWN_PENALTY",
    "BACKWARD_PAWN_PENALTY",
    "PASSED_PAWN_BONUS",
    "CONNECTED_PAWN_BONUS",
    "BISHOP_PAIR_BONUS",
    "KING_SHIELD_BONUS",
    "KING_OPEN_FILE_PENALTY",
    "KING_ZONE_ATTACK_WEIGHT",
    "MOBILITY_WEIGHT",
    "TT_SIZE_MB",
    "TT_MAX_ENTRIES",
    "TT_ENTRY_BYTES",
]
