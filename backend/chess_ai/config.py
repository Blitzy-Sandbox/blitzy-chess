"""Central configuration for the ``chess_ai`` backend package.

This module is the single shared-configuration surface for the package. It
defines move-pacing timing constants, the AI difficulty tiers, server/network
settings, filesystem paths, and transposition-table sizing. Application code,
the API layer, the self-play tooling, and the pure engine package all import
their configuration from here.

It uses the Python standard library only (``os``, ``dataclasses``,
``datetime``, ``pathlib``) and imports no web framework or chess library, so it
is safe to import from the pure-computation engine package as well as from the
FastAPI application layer.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Move-pacing timing constants (milliseconds)
# ---------------------------------------------------------------------------
MIN_AI_DELAY_MS: int = 1500
SELF_PLAY_MOVE_DELAY_MS: int = 5000

# Seconds equivalents derived from the canonical millisecond values above.
MIN_AI_DELAY_S: float = MIN_AI_DELAY_MS / 1000.0
SELF_PLAY_MOVE_DELAY_S: float = SELF_PLAY_MOVE_DELAY_MS / 1000.0


# ---------------------------------------------------------------------------
# AI difficulty tiers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DifficultyTier:
    """A single AI difficulty tier: its name, search depth, and time budget."""

    name: str
    depth: int
    time_budget_s: float

    @property
    def time_budget_ms(self) -> int:
        """Time budget expressed in whole milliseconds."""
        return int(self.time_budget_s * 1000)


DIFFICULTY_TIERS: dict[str, DifficultyTier] = {
    "easy": DifficultyTier("easy", 4, 3.0),
    "medium": DifficultyTier("medium", 6, 8.0),
    "hard": DifficultyTier("hard", 8, 15.0),
}


def get_tier(name: str) -> DifficultyTier:
    """Return the :class:`DifficultyTier` for ``name`` (case-insensitive).

    The lookup is keyed on the lowercase tier name, so the frontend's tier
    string maps directly.

    Args:
        name: Tier name such as ``"easy"``, ``"medium"``, or ``"hard"``.

    Returns:
        The matching :class:`DifficultyTier`.

    Raises:
        ValueError: If ``name`` does not match a known tier.
    """
    key = name.strip().lower()
    try:
        return DIFFICULTY_TIERS[key]
    except KeyError:
        valid = ", ".join(sorted(DIFFICULTY_TIERS))
        raise ValueError(f"Unknown difficulty tier {name!r}; valid tiers are: {valid}") from None


# ---------------------------------------------------------------------------
# Server / network configuration
# ---------------------------------------------------------------------------
BACKEND_HOST: str = os.environ.get("BACKEND_HOST", "0.0.0.0")
BACKEND_PORT: int = int(os.environ.get("BACKEND_PORT", os.environ.get("PORT", "8000")))

CORS_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


# ---------------------------------------------------------------------------
# Filesystem paths (derived from this file's location; cwd-independent)
# ---------------------------------------------------------------------------
BACKEND_ROOT: Path = Path(__file__).resolve().parent.parent
REPO_ROOT: Path = BACKEND_ROOT.parent

BOOKS_DIR: Path = BACKEND_ROOT / "books"
OPENING_BOOK_PATH: Path = BOOKS_DIR / "opening_book.bin"
TABLES_DIR: Path = BACKEND_ROOT / "tables"
GAMES_DIR: Path = BACKEND_ROOT / "games"

FRONTEND_DIST_DIR: Path = REPO_ROOT / "frontend" / "dist"


def self_play_recording_path(now: datetime | None = None) -> Path:
    """Build the self-play screen-recording path.

    Args:
        now: Timestamp to encode in the filename; defaults to the current time.

    Returns:
        A path of the form ``<GAMES_DIR>/self_play_YYYYMMDD_HHMMSS.mp4``.
    """
    timestamp = now if now is not None else datetime.now()
    return GAMES_DIR / f"self_play_{timestamp:%Y%m%d_%H%M%S}.mp4"


def ensure_dirs() -> None:
    """Create the runtime data directories (books, tables, games) if absent.

    This is an explicit callable invoked by application startup and the
    download scripts; it is intentionally not run at import time.
    """
    for directory in (BOOKS_DIR, TABLES_DIR, GAMES_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Transposition-table sizing
# ---------------------------------------------------------------------------
TT_SIZE_MB: int = 256
TT_MAX_ENTRIES: int = 2**20


# ---------------------------------------------------------------------------
# Aggregated settings namespace
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Immutable namespace bundling the individually exported settings.

    Importers may reference either the module-level constants directly or this
    ``settings`` instance (for example ``from chess_ai.config import settings``).
    Both refer to the same values.
    """

    MIN_AI_DELAY_MS: int = MIN_AI_DELAY_MS
    SELF_PLAY_MOVE_DELAY_MS: int = SELF_PLAY_MOVE_DELAY_MS
    BACKEND_HOST: str = BACKEND_HOST
    BACKEND_PORT: int = BACKEND_PORT
    CORS_ORIGINS: list[str] = field(default_factory=lambda: list(CORS_ORIGINS))
    DIFFICULTY_TIERS: dict[str, DifficultyTier] = field(
        default_factory=lambda: dict(DIFFICULTY_TIERS)
    )
    BOOKS_DIR: Path = BOOKS_DIR
    OPENING_BOOK_PATH: Path = OPENING_BOOK_PATH
    TABLES_DIR: Path = TABLES_DIR
    GAMES_DIR: Path = GAMES_DIR
    FRONTEND_DIST_DIR: Path = FRONTEND_DIST_DIR
    TT_SIZE_MB: int = TT_SIZE_MB
    TT_MAX_ENTRIES: int = TT_MAX_ENTRIES


settings = Settings()


__all__ = [
    "MIN_AI_DELAY_MS",
    "SELF_PLAY_MOVE_DELAY_MS",
    "MIN_AI_DELAY_S",
    "SELF_PLAY_MOVE_DELAY_S",
    "DifficultyTier",
    "DIFFICULTY_TIERS",
    "get_tier",
    "BACKEND_HOST",
    "BACKEND_PORT",
    "CORS_ORIGINS",
    "BACKEND_ROOT",
    "REPO_ROOT",
    "BOOKS_DIR",
    "OPENING_BOOK_PATH",
    "TABLES_DIR",
    "GAMES_DIR",
    "FRONTEND_DIST_DIR",
    "self_play_recording_path",
    "ensure_dirs",
    "TT_SIZE_MB",
    "TT_MAX_ENTRIES",
    "Settings",
    "settings",
]
