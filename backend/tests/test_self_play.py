"""Tests for the self-play package (``chess_ai.self_play``).

This suite covers two layers of the self-play demonstration:

* The PURE annotator (``chess_ai.self_play.annotator``) that turns a played
  game into the timestamped Markdown commentary transcript. These tests pin the
  Constraint-13 transcript contract: ``[MM:SS]`` timestamps, a WHY commentary
  carrying the evaluation components in centipawns, a top-3 alternatives
  section, and YouTube chapter markers. They are pure functions of their input,
  so they assert exact values and byte-for-byte determinism.
* The runner (``chess_ai.self_play.runner``): the pure async game loop
  ``play_self_play_game`` and the orchestration entrypoint ``run_self_play``.
  These tests pin the Constraint-14 behaviors: a deterministic terminal game,
  the >=5s/move pacing, the per-ply browser render hook, the move-limit cutoff,
  and the start -> record -> play -> transcript -> shutdown lifecycle.

Speed and isolation
-------------------
The suite MUST run headless and fast. Playwright, the browser, the screen
recording, and the FastAPI server are never started: the orchestration test
injects ``AsyncMock`` server and recorder seams and short-circuits the game.
The game-loop tests inject scripted searchers, a recording no-op ``sleep`` (so
the pacing never actually blocks), and a fast ``search`` adapter that skips the
real worker-thread offload. The module imports cleanly with NO Playwright
installed because the runner imports Playwright lazily, inside
``BrowserRecorder.start`` -- this file therefore never imports Playwright at the
top level.

The chess facts used below are verified and fixed: the Fool's-mate line
``1. f3 e5 2. g4 Qh4#`` ends in four plies with Black delivering checkmate, and
``config.SELF_PLAY_MOVE_DELAY_MS`` is 5000 milliseconds (5.0 seconds).
"""

import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import chess
import pytest

from chess_ai.config import SELF_PLAY_MOVE_DELAY_MS
from chess_ai.engine.evaluator import Evaluator
from chess_ai.self_play import runner
from chess_ai.self_play.annotator import (
    MoveAnnotation,
    build_chapters,
    format_timestamp,
    render_transcript,
    write_transcript,
)

# A fixed timestamp so every transcript rendered in the tests is reproducible.
FIXED_GENERATED_AT = datetime(2024, 1, 1, 12, 0, 0)

# The verified Fool's-mate UCI sequence: 1. f3 e5 2. g4 Qh4# (Black wins in 4).
WHITE_FOOLS_MATE = ["f2f3", "g2g4"]
BLACK_FOOLS_MATE = ["e7e5", "d8h4"]


# ---------------------------------------------------------------------------
# Test doubles: scripted searchers, a recording sleep, and a recording render
# ---------------------------------------------------------------------------
class ScriptedSearcher:
    """Return a fixed UCI move sequence, ignoring the position.

    Used to drive a fully deterministic game. Each call to :meth:`search`
    yields the next scripted move and a lightweight fake ``SearchResult`` with
    the fields the runner reads (``best_move``, ``score_cp``, ``ranked_moves``,
    and so on).
    """

    def __init__(self, ucis: list[str]) -> None:
        self._moves = [chess.Move.from_uci(uci) for uci in ucis]
        self._index = 0

    def search(self, board, limits=None, *args, **kwargs):
        """Return the next scripted move wrapped in a fake ``SearchResult``."""
        move = self._moves[self._index]
        self._index += 1
        return SimpleNamespace(
            best_move=move,
            score_cp=0,
            depth=1,
            pv=[move],
            nodes=1,
            time_s=0.0,
            ranked_moves=[(move, 0)],
            from_book=False,
            from_tablebase=False,
        )


class FirstLegalSearcher:
    """Return the first legal move in the position.

    This keeps every move legal while never steering toward a quick mate, so a
    small ``max_plies`` ceiling is what stops the game -- exactly what the
    move-limit test needs.
    """

    def search(self, board, limits=None, *args, **kwargs):
        """Return the first legal move wrapped in a fake ``SearchResult``."""
        move = next(iter(board.legal_moves))
        return SimpleNamespace(
            best_move=move,
            score_cp=0,
            depth=1,
            pv=[move],
            nodes=1,
            time_s=0.0,
            ranked_moves=[(move, 0)],
            from_book=False,
            from_tablebase=False,
        )


class RecordingSleep:
    """Async, non-blocking ``sleep`` that records every requested delay.

    Injected as the runner's ``sleep`` seam so the >=5s/move pacing is asserted
    from the recorded arguments instead of by actually waiting.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class RecordingRender:
    """Async render hook that records each per-ply browser ``state`` payload."""

    def __init__(self) -> None:
        self.states: list[dict] = []

    async def __call__(self, state: dict) -> None:
        self.states.append(state)


async def _search_adapter(searcher, board, limits):
    """Adapt a searcher to the runner's async ``search`` seam, synchronously.

    The runner offloads each search with ``await search(searcher, board,
    limits)``; this adapter calls the searcher directly and skips the real
    thread pool so the scripted game is instant and deterministic.
    """
    return searcher.search(board, limits)


def _make_annotation(
    *,
    ply: int,
    color: str,
    tier: str,
    san: str,
    elapsed_s: float,
    score_cp_white: int,
    phase: int = 24,
    alternatives: list[tuple[str, int]] | None = None,
    is_checkmate: bool = False,
) -> MoveAnnotation:
    """Build a representative :class:`MoveAnnotation` for the annotator tests."""
    return MoveAnnotation(
        ply=ply,
        move_number=(ply + 1) // 2,
        color=color,
        tier=tier,
        san=san,
        uci="0000",
        fen_before="startpos",
        fen_after="afterpos",
        elapsed_s=elapsed_s,
        score_cp_white=score_cp_white,
        components={
            "material": 10,
            "positional": 5,
            "pawns": 0,
            "king_safety": 0,
            "mobility": 5,
            "total": 20,
        },
        phase=phase,
        alternatives=alternatives if alternatives is not None else [],
        is_checkmate=is_checkmate,
        nodes=1000,
        depth=8,
    )


async def _play_fools_mate(
    *,
    sleep: RecordingSleep,
    render: RecordingRender | None = None,
    evaluator: Evaluator | None = None,
) -> tuple[list[MoveAnnotation], str, str]:
    """Drive the verified Fool's-mate game through ``play_self_play_game``.

    Scripted searchers play ``1. f3 e5 2. g4 Qh4#`` so the game ends in four
    plies with Black checkmating. The injected ``sleep`` records pacing and the
    injected ``search`` adapter keeps the run instant.
    """
    return await runner.play_self_play_game(
        white_searcher=ScriptedSearcher(WHITE_FOOLS_MATE),
        black_searcher=ScriptedSearcher(BLACK_FOOLS_MATE),
        evaluator=evaluator if evaluator is not None else Evaluator(),
        sleep=sleep,
        render=render,
        search=_search_adapter,
    )


# ---------------------------------------------------------------------------
# Annotator tests (pure; Constraint 13: transcript contract)
# ---------------------------------------------------------------------------
def test_format_timestamp() -> None:
    """``format_timestamp`` emits zero-padded ``[MM:SS]`` for the fixed values."""
    assert format_timestamp(0.0) == "[00:00]"
    assert format_timestamp(5.0) == "[00:05]"
    assert format_timestamp(65.0) == "[01:05]"
    assert format_timestamp(125.0) == "[02:05]"
    assert format_timestamp(3600.0) == "[60:00]"


def test_move_annotation_fields() -> None:
    """A constructed :class:`MoveAnnotation` round-trips its representative fields."""
    annotation = MoveAnnotation(
        ply=3,
        move_number=2,
        color="White",
        tier="Hard",
        san="Nf3",
        uci="g1f3",
        fen_before="rnbqkbnr/pppp1ppp/8/4p3/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 2",
        fen_after="rnbqkbnr/pppp1ppp/8/4p3/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 2",
        elapsed_s=12.5,
        score_cp_white=35,
        components={
            "material": 0,
            "positional": 20,
            "pawns": 5,
            "king_safety": 0,
            "mobility": 10,
            "total": 35,
        },
        phase=22,
        alternatives=[("e4", 40), ("d4", 30), ("c4", 25)],
        from_book=True,
        nodes=12345,
        depth=8,
    )

    assert annotation.ply == 3
    assert annotation.move_number == 2
    assert annotation.color == "White"
    assert annotation.san == "Nf3"
    assert annotation.uci == "g1f3"
    assert annotation.score_cp_white == 35
    # Evaluation components are carried in centipawns (Constraint 13).
    assert annotation.components["mobility"] == 10
    assert annotation.components["total"] == 35
    # Up to the top-3 alternatives are preserved as (san, score) pairs.
    assert len(annotation.alternatives) == 3
    assert annotation.alternatives[0] == ("e4", 40)
    assert annotation.from_book is True
    assert annotation.depth == 8
    assert annotation.nodes == 12345
    # The derived label uses "." for White (and would use "..." for Black).
    assert annotation.move_label == "2."


def test_build_chapters_starts_at_zero() -> None:
    """The YouTube chapter list always opens with ``("00:00", "Opening")``."""
    annotations = [
        _make_annotation(
            ply=1, color="White", tier="Hard", san="e4", elapsed_s=5.0, score_cp_white=20
        ),
        _make_annotation(
            ply=2, color="Black", tier="Medium", san="e5", elapsed_s=10.0, score_cp_white=-5
        ),
        _make_annotation(
            ply=3, color="White", tier="Hard", san="Nf3", elapsed_s=15.0, score_cp_white=15
        ),
    ]

    chapters = build_chapters(annotations)

    assert chapters  # non-empty
    assert chapters[0] == ("00:00", "Opening")


def test_render_transcript_is_deterministic() -> None:
    """A fixed ``generated_at`` makes ``render_transcript`` byte-for-byte stable."""
    annotations = [
        _make_annotation(
            ply=1,
            color="White",
            tier="Hard",
            san="f3",
            elapsed_s=5.0,
            score_cp_white=20,
            alternatives=[("e4", 20), ("d4", 15), ("Nf3", 12)],
        ),
        _make_annotation(
            ply=2, color="Black", tier="Medium", san="e5", elapsed_s=10.0, score_cp_white=-5
        ),
    ]

    first = render_transcript(
        annotations, generated_at=FIXED_GENERATED_AT, result="0-1", result_reason="checkmate"
    )
    second = render_transcript(
        annotations, generated_at=FIXED_GENERATED_AT, result="0-1", result_reason="checkmate"
    )

    assert isinstance(first, str)
    assert first == second


def test_render_transcript_contains_constraint13_elements() -> None:
    """The rendered transcript carries every load-bearing Constraint-13 element."""
    annotations = [
        _make_annotation(
            ply=1,
            color="White",
            tier="Hard",
            san="f3",
            elapsed_s=5.0,
            score_cp_white=20,
            alternatives=[("e4", 20), ("d4", 15), ("Nf3", 12)],
        ),
        _make_annotation(
            ply=2,
            color="Black",
            tier="Medium",
            san="Qh4",
            elapsed_s=10.0,
            score_cp_white=-9999,
            alternatives=[("c5", -3)],
            is_checkmate=True,
        ),
    ]

    text = render_transcript(
        annotations, generated_at=FIXED_GENERATED_AT, result="0-1", result_reason="checkmate"
    )

    # At least one [MM:SS] timestamp keyed to the video timeline.
    assert re.search(r"\[\d{2}:\d{2}\]", text)
    # WHY commentary with the evaluation components stated in centipawns.
    assert "Components (cp, White POV):" in text
    assert "cp" in text
    # A top-3 alternatives section.
    assert "Top alternatives:" in text
    # YouTube chapter markers, opening at 00:00.
    assert "## YouTube Chapters" in text
    assert "00:00 Opening" in text


def test_write_transcript_writes_utf8_md(tmp_path) -> None:
    """``write_transcript`` writes a non-empty UTF-8 ``.md`` file at the target."""
    annotations = [
        _make_annotation(
            ply=1, color="White", tier="Hard", san="e4", elapsed_s=5.0, score_cp_white=20
        ),
    ]
    target = tmp_path / "self_play_20240101_120000.md"

    written = write_transcript(
        annotations,
        target,
        recording_filename="self_play_20240101_120000.mp4",
        result="0-1",
        result_reason="checkmate",
        generated_at=FIXED_GENERATED_AT,
    )

    assert written == target
    assert target.exists()
    assert target.suffix == ".md"
    content = target.read_text(encoding="utf-8")
    assert content  # non-empty
    assert "Self-Play Commentary" in content
    assert "e4" in content


# ---------------------------------------------------------------------------
# Game-loop tests (``play_self_play_game``; Constraint 14)
# ---------------------------------------------------------------------------
def test_module_imports_without_playwright() -> None:
    """The runner module imports with no Playwright (lazy import) and exposes its API."""
    # Importing happened at the top of this file with no top-level Playwright
    # import; the runner only imports Playwright lazily inside its recorder.
    assert hasattr(runner, "play_self_play_game")
    assert hasattr(runner, "run_self_play")
    assert callable(runner.play_self_play_game)
    assert callable(runner.run_self_play)
    # The lazy import means Playwright is not bound at module scope.
    assert not hasattr(runner, "playwright")
    assert not hasattr(runner, "async_playwright")


async def test_play_self_play_game_reaches_checkmate() -> None:
    """The scripted Fool's-mate game ends in four plies with Black checkmating."""
    sleep = RecordingSleep()

    outcome = await _play_fools_mate(sleep=sleep)

    assert isinstance(outcome, tuple)
    assert len(outcome) == 3
    annotations, result_str, result_reason = outcome

    # Four plies were played: 1. f3 e5 2. g4 Qh4#.
    assert len(annotations) == 4
    assert all(isinstance(item, MoveAnnotation) for item in annotations)
    # python-chess result string for a Black win, with the matching reason.
    assert result_str == "0-1"
    assert result_reason == "checkmate"
    # The final move is Black's mating move.
    assert annotations[-1].is_checkmate is True
    assert annotations[-1].color == "Black"
    assert annotations[-1].san.endswith("#")
    # Hard plays White and Medium plays Black, alternating each ply.
    assert [item.color for item in annotations] == ["White", "Black", "White", "Black"]
    assert [item.tier for item in annotations] == ["Hard", "Medium", "Hard", "Medium"]
    # The real evaluator filled the WHITE-POV component breakdown end to end.
    assert set(annotations[0].components) >= {
        "material",
        "positional",
        "pawns",
        "king_safety",
        "mobility",
        "total",
    }


async def test_play_self_play_game_paces_each_move() -> None:
    """Every move is paced at the >=5s/move budget (Constraint 14)."""
    # The canonical pacing constant is 5000 ms (5.0 s).
    assert SELF_PLAY_MOVE_DELAY_MS == 5000

    sleep = RecordingSleep()
    annotations, _result_str, _result_reason = await _play_fools_mate(sleep=sleep)

    # One pacing sleep was awaited for each ply played.
    assert len(sleep.calls) == len(annotations) == 4

    expected_s = SELF_PLAY_MOVE_DELAY_MS / 1000.0  # 5.0
    for delay in sleep.calls:
        # The runner subtracts the tiny move-computation time, so each delay is
        # just under the full budget but never exceeds it.
        assert 0 < delay <= expected_s
        assert delay == pytest.approx(expected_s, abs=0.5)


async def test_play_self_play_game_invokes_render_per_ply() -> None:
    """The render hook is invoked once per ply with the per-move board state."""
    sleep = RecordingSleep()
    render = RecordingRender()

    annotations, _result_str, _result_reason = await _play_fools_mate(sleep=sleep, render=render)

    # The hook fired once per ply played.
    assert len(render.states) == len(annotations) == 4
    # Each payload carries the state the frontend self-play view consumes.
    first_state = render.states[0]
    assert first_state["fen"]
    assert first_state["san"]
    assert "lastMove" in first_state


async def test_play_self_play_game_respects_max_plies() -> None:
    """A small ``max_plies`` ceiling stops a non-terminating game without a mate."""
    sleep = RecordingSleep()
    # A fake evaluator (isolation) exposing the WHITE-POV component breakdown.
    fake_evaluator = MagicMock()
    fake_evaluator.evaluate_components.return_value = SimpleNamespace(
        material=0,
        positional=0,
        pawns=0,
        king_safety=0,
        mobility=0,
        total=0,
        phase=24,
    )

    annotations, result_str, result_reason = await runner.play_self_play_game(
        white_searcher=FirstLegalSearcher(),
        black_searcher=FirstLegalSearcher(),
        evaluator=fake_evaluator,
        sleep=sleep,
        search=_search_adapter,
        max_plies=2,
    )

    # The loop stopped exactly at the ceiling, with an unfinished game.
    assert len(annotations) == 2
    assert result_str == "*"
    assert result_reason == "move limit reached"
    # The evaluator's component breakdown was consulted once per ply.
    assert fake_evaluator.evaluate_components.call_count == 2


# ---------------------------------------------------------------------------
# Orchestration test (``run_self_play``; Constraint 14 lifecycle, fully mocked)
# ---------------------------------------------------------------------------
async def test_run_self_play_orchestrates_with_mocks(tmp_path, monkeypatch) -> None:
    """``run_self_play`` runs start -> record -> play -> transcript -> shutdown."""
    recording_path = tmp_path / "self_play_20240101_120000.mp4"

    # Redirect all artifact output into tmp_path, never the repo's backend/games/.
    monkeypatch.setattr(runner.config, "self_play_recording_path", lambda now=None: recording_path)
    # Keep resource loading cheap and offline (no book / tablebase files needed).
    monkeypatch.setattr(runner, "load_book", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "open_tablebase", lambda *args, **kwargs: None)

    # Short-circuit the otherwise slow, real-search game with a scripted result.
    sample = _make_annotation(
        ply=1, color="White", tier="Hard", san="e4", elapsed_s=5.0, score_cp_white=20
    )

    async def fake_play(**kwargs):
        return [sample], "0-1", "checkmate"

    monkeypatch.setattr(runner, "play_self_play_game", fake_play)

    # Mock the awaited server and recorder lifecycle seams (no real browser/server).
    server = AsyncMock()
    recorder = AsyncMock()
    recorder.stop_and_save.return_value = recording_path

    summary = await runner.run_self_play(now=FIXED_GENERATED_AT, server=server, recorder=recorder)

    # Lifecycle: the server and recorder were started, used, and shut down.
    assert server.start.await_count == 1
    assert recorder.start.await_count == 1
    assert recorder.stop_and_save.await_count == 1
    assert recorder.cleanup.await_count == 1
    assert server.stop.await_count == 1

    # Summary shape carries the artifact paths and the game result.
    assert set(summary) == {"recording", "transcript", "result", "reason", "moves"}
    assert summary["result"] == "0-1"
    assert summary["reason"] == "checkmate"
    assert summary["moves"] == 1

    # The recording path follows self_play_YYYYMMDD_HHMMSS.mp4.
    assert re.fullmatch(r"self_play_\d{8}_\d{6}\.mp4", summary["recording"].name)

    # The transcript was written next to the recording as a UTF-8 .md file.
    transcript = summary["transcript"]
    assert transcript.suffix == ".md"
    assert transcript.exists()
    assert transcript.read_text(encoding="utf-8")
