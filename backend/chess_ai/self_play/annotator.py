"""Self-play commentary transcript writer for the blitzy-chess backend.

This module turns a self-play game into an annotated, timestamped Markdown
transcript that sits next to the recorded video. It implements the transcript
side of the self-play demonstration: ``[MM:SS]`` timestamps that map to the
video timeline, a per-move WHY commentary that states the evaluation in
centipawns, the top-3 alternatives the search considered, and a YouTube
chapter list.

The annotator is the leaf of the ``self_play`` subpackage. The runner produces
a list of :class:`MoveAnnotation` records (SAN, FEN, evaluation numbers, search
statistics, and timing) and hands them to :func:`write_transcript`; everything
in this module is a pure function of that input.

Purity
------
Standard library only. This module imports ``dataclasses``, ``datetime``,
``os``, and ``pathlib`` and nothing else. It does not import ``chess``
(python-chess), the ``chess_ai.engine`` package, ``chess_ai.config``,
Playwright, or any web framework, so it stays trivially importable and
unit-testable with no heavy dependencies.

Determinism
-----------
Every rendering function returns a string built entirely from its arguments.
The single non-deterministic value is the default ``generated_at`` timestamp
in :func:`render_transcript`; pass an explicit ``generated_at`` for
byte-identical output. The only filesystem access lives in
:func:`write_transcript`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from os import PathLike
from pathlib import Path


# ---------------------------------------------------------------------------
# Per-move record (the contract shared with runner.py)
# ---------------------------------------------------------------------------
@dataclass
class MoveAnnotation:
    """One half-move with everything needed to render its commentary entry.

    The runner constructs these records as it plays the demonstration game and
    passes the ordered list to the transcript functions. All evaluation numbers
    are pre-computed by the runner so this module never touches the engine.

    Fields:
        ply: 1-based half-move index (1 is White's first move).
        move_number: Full-move number, ``(ply + 1) // 2``.
        color: ``"White"`` or ``"Black"`` (the side that made this move).
        tier: Difficulty tier that produced the move (``"Hard"`` plays White,
            ``"Medium"`` plays Black in the demonstration).
        san: Standard Algebraic Notation of the move, e.g. ``"Nf3"``, ``"exd5"``,
            ``"O-O"``, ``"e8=Q+"``.
        uci: Coordinate/UCI form of the move, e.g. ``"g1f3"``.
        fen_before: FEN of the position before the move.
        fen_after: FEN of the position after the move.
        elapsed_s: Seconds from game start to when the move was shown on screen.
        score_cp_white: Headline evaluation in centipawns, White POV (positive
            favors White). The runner normalizes the search score to White POV.
        components: Static evaluation components in centipawns, White POV, keyed
            by ``"material"``, ``"positional"``, ``"pawns"``, ``"king_safety"``,
            and ``"mobility"`` (and optionally ``"total"``).
        phase: Game phase 0..24 (24 is full midgame, 0 is a bare endgame), or
            ``None`` when unavailable.
        alternatives: Up to the top-3 alternatives as ``(san, score_cp)`` pairs,
            with the score in mover POV (the search's native convention).
        is_capture: Whether the move captured a piece.
        is_check: Whether the move gives check.
        is_checkmate: Whether the move delivers checkmate.
        from_book: Whether the move came from the opening book.
        from_tablebase: Whether the move came from a Syzygy tablebase.
        nodes: Nodes searched for the move, or ``None`` (book/tablebase moves).
        depth: Search depth reached for the move, or ``None``.
    """

    ply: int
    move_number: int
    color: str
    tier: str
    san: str
    uci: str
    fen_before: str
    fen_after: str
    elapsed_s: float
    score_cp_white: int
    components: dict[str, int] = field(default_factory=dict)
    phase: int | None = None
    alternatives: list[tuple[str, int]] = field(default_factory=list)
    is_capture: bool = False
    is_check: bool = False
    is_checkmate: bool = False
    from_book: bool = False
    from_tablebase: bool = False
    nodes: int | None = None
    depth: int | None = None

    @property
    def move_label(self) -> str:
        """Return the move number with ``.`` for White or ``...`` for Black."""
        separator = "." if self.color == "White" else "..."
        return f"{self.move_number}{separator}"


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------
_COMPONENT_LABELS: list[tuple[str, str]] = [
    ("material", "material"),
    ("positional", "position"),
    ("pawns", "pawns"),
    ("king_safety", "king safety"),
    ("mobility", "mobility"),
]


def _component_cp(components: dict[str, int], key: str) -> int:
    """Return ``components[key]`` as an int, treating missing/None as 0."""
    value = components.get(key)
    if value is None:
        return 0
    return int(value)


def _assessment(score_cp_white: int) -> str:
    """Return a verdict for a White-POV centipawn score.

    A score within +/-50 centipawns reads as ``"roughly equal"``; otherwise it
    favors the side with the larger value.
    """
    if score_cp_white >= 50:
        return "White is better"
    if score_cp_white <= -50:
        return "Black is better"
    return "roughly equal"


# ---------------------------------------------------------------------------
# Timestamp formatting (Constraint 13: [MM:SS])
# ---------------------------------------------------------------------------
def format_clock(elapsed_s: float) -> str:
    """Format elapsed seconds as a bare ``"MM:SS"`` timecode.

    Minutes are not capped at 59, so a long game reads as ``"73:20"``. This is
    the form YouTube chapter lines require (a leading ``0:00`` timecode).

    Args:
        elapsed_s: Seconds since the game started. Negative inputs clamp to 0.

    Returns:
        A zero-padded ``"MM:SS"`` string.
    """
    total = max(0, int(elapsed_s))
    minutes = total // 60
    seconds = total % 60
    return f"{minutes:02d}:{seconds:02d}"


def format_timestamp(elapsed_s: float) -> str:
    """Format elapsed seconds as a bracketed ``"[MM:SS]"`` timestamp.

    Args:
        elapsed_s: Seconds since the game started.

    Returns:
        The :func:`format_clock` value wrapped in square brackets, e.g.
        ``"[02:05]"``.
    """
    return f"[{format_clock(elapsed_s)}]"


# ---------------------------------------------------------------------------
# WHY commentary (Constraint 13: WHY + eval components in centipawns)
# ---------------------------------------------------------------------------
def why_line(ann: MoveAnnotation) -> str:
    """Build the one-line WHY commentary for a move.

    The line states the headline evaluation in both centipawns and pawn units,
    lists the evaluation components in centipawns (White POV), names the move
    source (book, tablebase, or search statistics), and notes capture/check/
    checkmate flags. It is a pure function of ``ann``.

    Args:
        ann: The move record to describe.

    Returns:
        A plain-English commentary string.
    """
    score = int(ann.score_cp_white)
    parts: list[str] = [
        f"Eval {score:+d}cp ({score / 100:+.2f}), {_assessment(score)}.",
    ]

    breakdown = ", ".join(
        f"{label} {_component_cp(ann.components, key):+d}" for key, label in _COMPONENT_LABELS
    )
    parts.append(f"Components (cp, White POV): {breakdown}.")

    if ann.from_book:
        parts.append("Book move.")
    if ann.from_tablebase:
        parts.append("Tablebase-confirmed.")
    if not ann.from_book and not ann.from_tablebase:
        depth = ann.depth if ann.depth is not None else "n/a"
        nodes = ann.nodes if ann.nodes is not None else "n/a"
        parts.append(f"(depth {depth}, {nodes} nodes)")

    if ann.is_capture:
        parts.append("Capture.")
    if ann.is_checkmate:
        parts.append("Checkmate!")
    elif ann.is_check:
        parts.append("Check.")

    return " ".join(parts)


def format_alternatives(ann: MoveAnnotation) -> list[str]:
    """Render the top-3 alternatives as numbered human-readable lines.

    Scores are in mover POV (the search's native convention) and are rendered
    with an explicit sign. An empty ``alternatives`` list yields an empty list,
    and the renderer omits the section.

    Args:
        ann: The move record whose ``alternatives`` are rendered.

    Returns:
        Lines such as ``["1. e4 (+20)", "2. d4 (+15)", "3. Nf3 (+12)"]``.
    """
    lines: list[str] = []
    for index, (san, score) in enumerate(ann.alternatives[:3], start=1):
        lines.append(f"{index}. {san} ({int(score):+d})")
    return lines


# ---------------------------------------------------------------------------
# YouTube chapter markers (Constraint 13)
# ---------------------------------------------------------------------------
# Higher value wins when two chapters land on the same timecode.
_CHAPTER_PRIORITY: dict[str, int] = {
    "Opening": 0,
    "Middlegame": 1,
    "Game over": 2,
    "Endgame": 3,
    "Checkmate": 4,
}

# Thresholds that mark the transition between game stages.
_MIDGAME_MOVE_NUMBER = 11
_MIDGAME_PHASE = 18
_ENDGAME_PHASE = 6


def _timecode_to_seconds(timecode: str) -> int:
    """Convert a bare ``"MM:SS"`` timecode back to total seconds for sorting."""
    minutes, seconds = timecode.split(":")
    return int(minutes) * 60 + int(seconds)


def _normalize_chapters(raw: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort, de-duplicate, and pin the chapter list to YouTube's rules.

    ``"00:00"`` is forced to ``"Opening"``. When two chapters share a timecode
    the higher-priority label wins. The result is ordered by ascending time
    with no duplicate timecodes.
    """
    best: dict[str, tuple[int, str]] = {}
    for timecode, label in raw:
        if timecode == "00:00":
            best[timecode] = (_CHAPTER_PRIORITY["Opening"], "Opening")
            continue
        priority = _CHAPTER_PRIORITY.get(label, 0)
        current = best.get(timecode)
        if current is None or priority > current[0]:
            best[timecode] = (priority, label)
    ordered = sorted(best.items(), key=lambda item: _timecode_to_seconds(item[0]))
    return [(timecode, label) for timecode, (_priority, label) in ordered]


def build_chapters(annotations: list[MoveAnnotation]) -> list[tuple[str, str]]:
    """Build an ordered YouTube chapter list from the move annotations.

    The list always opens with ``("00:00", "Opening")``. It adds a
    ``"Middlegame"`` chapter at the first move that has left the opening
    (full-move number at least 11, or a phase that has dropped to the midgame
    threshold), an ``"Endgame"`` chapter at the first low-phase move after the
    middlegame, and a final ``"Checkmate"`` chapter at the mating move (or a
    ``"Game over"`` chapter at the last move otherwise). Timecodes are bare
    ``"MM:SS"`` strings, sorted ascending and de-duplicated.

    Args:
        annotations: The ordered move records for the game.

    Returns:
        ``(timecode, label)`` tuples suitable for a YouTube description.
    """
    chapters: list[tuple[str, str]] = [("00:00", "Opening")]
    if not annotations:
        return chapters

    midgame_index: int | None = None
    for index, ann in enumerate(annotations):
        left_opening = ann.move_number >= _MIDGAME_MOVE_NUMBER
        low_phase = ann.phase is not None and ann.phase <= _MIDGAME_PHASE
        if left_opening or low_phase:
            midgame_index = index
            break

    if midgame_index is not None:
        chapters.append((format_clock(annotations[midgame_index].elapsed_s), "Middlegame"))
        for ann in annotations[midgame_index:]:
            if ann.phase is not None and ann.phase <= _ENDGAME_PHASE:
                chapters.append((format_clock(ann.elapsed_s), "Endgame"))
                break

    mate = next((ann for ann in annotations if ann.is_checkmate), None)
    if mate is not None:
        chapters.append((format_clock(mate.elapsed_s), "Checkmate"))
    else:
        chapters.append((format_clock(annotations[-1].elapsed_s), "Game over"))

    return _normalize_chapters(chapters)


# ---------------------------------------------------------------------------
# Full transcript rendering (pure -> string)
# ---------------------------------------------------------------------------
def render_transcript(
    annotations: list[MoveAnnotation],
    *,
    white_tier: str = "Hard",
    black_tier: str = "Medium",
    recording_filename: str | None = None,
    result: str | None = None,
    result_reason: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render the complete Markdown transcript for a self-play game.

    The document carries a title, a metadata block, a YouTube chapter list, and
    a move-by-move commentary section. Given the same arguments (including an
    explicit ``generated_at``) it returns byte-identical output.

    Args:
        annotations: The ordered move records for the game.
        white_tier: Tier label for the White side.
        black_tier: Tier label for the Black side.
        recording_filename: Name of the recorded video, listed in the metadata
            block when provided.
        result: Result string such as ``"1-0"``, ``"0-1"``, or ``"1/2-1/2"``.
        result_reason: Reason such as ``"checkmate"`` or ``"move limit
            reached"``.
        generated_at: Timestamp stamped into the metadata block; defaults to
            :func:`datetime.now` only when ``None`` (the single
            non-deterministic value).

    Returns:
        The Markdown transcript, terminated by a trailing newline.
    """
    if generated_at is None:
        generated_at = datetime.now()

    lines: list[str] = [
        f"# Self-Play Commentary \u2014 {white_tier} (White) vs {black_tier} (Black)",
        "",
        "## Game Metadata",
        "",
    ]
    if recording_filename is not None:
        lines.append(f"- Recording: {recording_filename}")
    lines.append(f"- Generated: {generated_at.isoformat()}")
    lines.append(f"- Moves: {len(annotations)}")
    result_text = result if result is not None else "n/a"
    if result_reason:
        result_text = f"{result_text} ({result_reason})"
    lines.append(f"- Result: {result_text}")
    lines.append("")

    lines.append("## YouTube Chapters")
    lines.append("")
    lines.append("```")
    for timecode, label in build_chapters(annotations):
        lines.append(f"{timecode} {label}")
    lines.append("```")
    lines.append("")

    lines.append("## Move-by-Move Commentary")
    lines.append("")
    if not annotations:
        lines.append("No moves were played.")
        lines.append("")
    else:
        for ann in annotations:
            header = (
                f"### {ann.move_label} {ann.san} {format_timestamp(ann.elapsed_s)} "
                f"\u2014 {ann.tier} ({ann.color})"
            )
            lines.append(header)
            lines.append("")
            lines.append(why_line(ann))
            lines.append("")
            alternatives = format_alternatives(ann)
            if alternatives:
                lines.append("Top alternatives:")
                lines.append("")
                for entry in alternatives:
                    lines.append(f"- {entry}")
                lines.append("")
            lines.append(f"FEN: `{ann.fen_after}`")
            lines.append("")

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


# ---------------------------------------------------------------------------
# Path helper and file writing (the only filesystem access)
# ---------------------------------------------------------------------------
def transcript_path_for(recording_path: str | PathLike[str]) -> Path:
    """Map a recording path to its sibling transcript path.

    The transcript keeps the recording's stem and swaps the suffix to ``.md``,
    so ``self_play_YYYYMMDD_HHMMSS.mp4`` becomes
    ``self_play_YYYYMMDD_HHMMSS.md`` in the same directory.

    Args:
        recording_path: The video path as a string or ``os.PathLike``.

    Returns:
        The transcript :class:`~pathlib.Path`.
    """
    return Path(recording_path).with_suffix(".md")


def write_transcript(
    annotations: list[MoveAnnotation],
    transcript_path: str | PathLike[str],
    *,
    white_tier: str = "Hard",
    black_tier: str = "Medium",
    recording_filename: str | None = None,
    result: str | None = None,
    result_reason: str | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """Render the transcript and write it to ``transcript_path`` as UTF-8.

    The parent directory is created when missing. When ``recording_filename``
    is not given it defaults to the transcript's name with a ``.mp4`` suffix.

    Args:
        annotations: The ordered move records for the game.
        transcript_path: Destination ``.md`` path (string or ``os.PathLike``).
        white_tier: Tier label for the White side.
        black_tier: Tier label for the Black side.
        recording_filename: Name of the recorded video for the metadata block.
        result: Result string such as ``"1-0"``.
        result_reason: Reason such as ``"checkmate"``.
        generated_at: Timestamp for the metadata block; defaults to the current
            time when ``None``.

    Returns:
        The :class:`~pathlib.Path` that was written.
    """
    path = Path(transcript_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if recording_filename is None:
        recording_filename = path.with_suffix(".mp4").name
    text = render_transcript(
        annotations,
        white_tier=white_tier,
        black_tier=black_tier,
        recording_filename=recording_filename,
        result=result,
        result_reason=result_reason,
        generated_at=generated_at,
    )
    path.write_text(text, encoding="utf-8")
    return path


class Annotator:
    """Object-oriented call site over the transcript functions.

    This is a thin convenience wrapper; :func:`transcript_path_for` and
    :func:`write_transcript` are the source of truth.
    """

    def write(
        self,
        annotations: list[MoveAnnotation],
        recording_path: str | PathLike[str],
        **meta: object,
    ) -> Path:
        """Write the transcript next to ``recording_path``.

        Args:
            annotations: The ordered move records for the game.
            recording_path: The recorded video path; the transcript is written
                to the sibling ``.md`` path.
            **meta: Keyword metadata forwarded to :func:`write_transcript`
                (``white_tier``, ``black_tier``, ``recording_filename``,
                ``result``, ``result_reason``, ``generated_at``).

        Returns:
            The :class:`~pathlib.Path` that was written.
        """
        transcript_path = transcript_path_for(recording_path)
        return write_transcript(annotations, transcript_path, **meta)


__all__ = [
    "Annotator",
    "MoveAnnotation",
    "build_chapters",
    "format_alternatives",
    "format_clock",
    "format_timestamp",
    "render_transcript",
    "transcript_path_for",
    "why_line",
    "write_transcript",
]
