"""Self-play demonstration orchestrator for the blitzy-chess backend.

This module drives the AI self-play demonstration end to end and is the target
of ``make self-play`` (``python -m chess_ai.self_play.runner`` with the working
directory ``backend/``). The pipeline runs in a fixed order: start the FastAPI
server as a uvicorn subprocess, open a Playwright Chromium browser at the
``/self-play`` route, begin screen recording, play a Hard-versus-Medium game
pacing at no less than five seconds per move, stop the recording and save it to
``config.self_play_recording_path(now)``, write the commentary transcript next
to it, and shut everything down. Shutdown runs even on error.

The engine search is synchronous, so every search and every
``Evaluator.evaluate_components`` call is offloaded with ``asyncio.to_thread``
and receives a ``board.copy()`` (``chess.Board`` is not thread-safe). This
module imports the engine but the engine never imports back. Playwright is
imported lazily inside :class:`BrowserRecorder` so importing this module (and
its package) never requires Playwright to be installed.

Browser render contract
------------------------
The frontend ``/self-play`` view implements a window hook the runner calls:

* Readiness: ``window.__BLITZY_SELF_PLAY__.ready === true`` once the view is
  mounted and ready to render.
* Rendering: ``window.__BLITZY_SELF_PLAY__.render(state)`` is called once per
  move with a ``state`` object carrying ``fen`` (position after the move),
  ``lastMove`` (``{from, to, uci}`` UCI squares), ``moveNumber``,
  ``sideToMove`` (``"white"``/``"black"``), ``san``, ``whiteTier``,
  ``blackTier``, ``evalCp`` (centipawns, White POV), and ``status``
  (``"playing"``, ``"check"``, ``"checkmate"``, or ``"gameover"``).

The hook is best-effort: a missing or not-yet-ready hook never aborts the game,
the recording, the transcript, or shutdown. ``make self-play`` does not build
the frontend, so the full visual UI requires ``config.FRONTEND_DIST_DIR`` to be
present (build it with ``make build``); when it is absent the page may be blank
or return 404 and the pipeline still completes with recording and transcript.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import chess

from chess_ai import config
from chess_ai.engine import (
    Evaluator,
    Searcher,
    SearchLimits,
    SearchResult,
    load_book,
    open_tablebase,
)
from chess_ai.self_play.annotator import (
    MoveAnnotation,
    transcript_path_for,
    write_transcript,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser render-hook JavaScript (the contract the frontend implements)
# ---------------------------------------------------------------------------
_READY_HOOK_JS: str = (
    "() => window.__BLITZY_SELF_PLAY__ && window.__BLITZY_SELF_PLAY__.ready === true"
)
_RENDER_HOOK_JS: str = (
    "(state) => { if (window.__BLITZY_SELF_PLAY__ && window.__BLITZY_SELF_PLAY__.render) "
    "{ window.__BLITZY_SELF_PLAY__.render(state); } }"
)


# ---------------------------------------------------------------------------
# Injectable callable seams (typed for the pure game-driver)
# ---------------------------------------------------------------------------
# A single-search offloader: given a searcher, a board copy, and limits, returns
# an awaitable resolving to a SearchResult.
SearchCallable = Callable[[Searcher, chess.Board, SearchLimits], Awaitable[SearchResult]]
# A per-move browser render callback receiving the state dict.
RenderCallable = Callable[[dict], Awaitable[None]]
# An async sleep callback receiving a delay in seconds.
SleepCallable = Callable[[float], Awaitable[None]]


# ---------------------------------------------------------------------------
# Backend server lifecycle (uvicorn subprocess)
# ---------------------------------------------------------------------------
class ServerProcess:
    """Manage the uvicorn subprocess that serves ``chess_ai.app:app``.

    The subprocess is launched with the active interpreter and torn down on
    :meth:`stop`. Readiness is detected by polling the ``/health`` endpoint.
    The ``popen`` factory is injectable so tests can substitute a fake process.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = config.BACKEND_PORT,
        cwd: Path = config.BACKEND_ROOT,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        health_path: str = "/health",
    ) -> None:
        """Store the launch parameters; no process is started until :meth:`start`.

        Args:
            host: Interface the server binds to.
            port: TCP port the server listens on.
            cwd: Working directory for the subprocess (the ``backend/`` root so
                ``chess_ai`` is importable).
            popen: Factory used to spawn the process; defaults to
                :class:`subprocess.Popen` and is injectable for tests.
            health_path: Path polled for readiness.
        """
        self.host = host
        self.port = port
        self.cwd = cwd
        self._popen = popen
        self.health_path = health_path
        self._proc: subprocess.Popen | None = None

    @property
    def health_url(self) -> str:
        """Return the absolute readiness URL built from host, port, and path."""
        return f"http://{self.host}:{self.port}{self.health_path}"

    def _probe_once(self) -> bool:
        """Return ``True`` when a single GET to :attr:`health_url` returns 2xx.

        Any connection error, timeout, or non-2xx status returns ``False``. This
        is a blocking call; the coroutine runs it via ``asyncio.to_thread``.
        """
        try:
            with urllib.request.urlopen(self.health_url, timeout=2) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                return 200 <= int(status) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    async def start(self, *, timeout_s: float = 30.0, poll_interval_s: float = 0.5) -> None:
        """Launch the server and wait until it answers the health probe.

        Args:
            timeout_s: Maximum seconds to wait for readiness.
            poll_interval_s: Delay between readiness probes.

        Raises:
            RuntimeError: If the subprocess exits early or readiness is not
                reached within ``timeout_s``.
        """
        if self._proc is not None and self._proc.poll() is None:
            return

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._proc = self._popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "chess_ai.app:app",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            cwd=str(self.cwd),
            env=env,
        )

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                raise RuntimeError(f"uvicorn exited early with code {code}")
            if await asyncio.to_thread(self._probe_once):
                logger.info("server ready at %s", self.health_url)
                return
            await asyncio.sleep(poll_interval_s)

        raise RuntimeError(
            f"server did not become ready within {timeout_s:.0f}s at {self.health_url}"
        )

    async def stop(self) -> None:
        """Terminate the server, escalating to kill, and never raise.

        Safe to call more than once; subsequent calls are no-ops.
        """
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            self._proc = None
            return
        try:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 10)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait, 5)
        except Exception:
            logger.exception("error while stopping the server subprocess")
        finally:
            self._proc = None

    async def __aenter__(self) -> "ServerProcess":
        """Start the server and return ``self`` for ``async with`` use."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Stop the server on exit from an ``async with`` block."""
        await self.stop()


# ---------------------------------------------------------------------------
# Recording finalization (WebM -> MP4 via a capable ffmpeg)
# ---------------------------------------------------------------------------
class RecordingFinalizationError(RuntimeError):
    """Raised when the recorded WebM cannot be finalized into a real MP4.

    The pipeline raises this rather than emitting a file with a ``.mp4`` name
    that is not an MP4 container, so a failure is loud instead of producing a
    mislabeled artifact (Constraint 14 requires a genuine MP4 recording).
    """


# ffmpeg invocations are bounded so a hung binary never stalls the pipeline.
_FFMPEG_PROBE_TIMEOUT_S: float = 15.0
_FFMPEG_TRANSCODE_TIMEOUT_S: float = 300.0


def _playwright_cache_dir() -> Path | None:
    """Return the Playwright browser cache directory for this platform.

    Honors Playwright's own ``PLAYWRIGHT_BROWSERS_PATH`` override and otherwise
    falls back to the per-OS default. Returns ``None`` when no such directory
    exists.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        candidate = Path(override)
        return candidate if candidate.is_dir() else None

    home = Path.home()
    if sys.platform == "darwin":
        cache = home / "Library" / "Caches" / "ms-playwright"
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        cache = base / "ms-playwright"
    else:
        cache = home / ".cache" / "ms-playwright"
    return cache if cache.is_dir() else None


def _bundled_ffmpeg_candidates() -> list[Path]:
    """List the ffmpeg executables Playwright bundles in its browser cache.

    Playwright ships ffmpeg under ``ffmpeg-<build>/ffmpeg-<platform>``; this
    globs every matching executable so discovery can probe each one.
    """
    cache = _playwright_cache_dir()
    if cache is None:
        return []
    return [
        path
        for path in sorted(cache.glob("ffmpeg-*/ffmpeg-*"))
        if path.is_file() and os.access(path, os.X_OK)
    ]


def _run_ffmpeg(
    args: list[str], *, timeout_s: float = _FFMPEG_TRANSCODE_TIMEOUT_S
) -> subprocess.CompletedProcess:
    """Run an ffmpeg command and capture its output.

    The single seam through which both the muxer probe and the transcode call
    ffmpeg, so tests can substitute the whole subprocess interaction.
    """
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)


def _ffmpeg_supports_mp4(ffmpeg: str) -> bool:
    """Report whether ``ffmpeg`` advertises an MP4 muxer.

    Playwright's bundled ffmpeg is built with only the WebM and image muxers, so
    its mere presence is not enough; this probes ``-muxers`` for an MP4 muxing
    entry. Any probe error is treated as "not capable".
    """
    try:
        completed = _run_ffmpeg(
            [ffmpeg, "-hide_banner", "-muxers"], timeout_s=_FFMPEG_PROBE_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        # A muxing-capable row looks like ``  E  mp4  MP4 (MPEG-4 Part 14)``:
        # the flag field carries ``E`` and the format field names ``mp4``.
        if len(parts) >= 2 and "E" in parts[0] and "mp4" in parts[1].split(","):
            return True
    return False


def _discover_mp4_capable_ffmpeg() -> str | None:
    """Find an ffmpeg that can actually mux MP4, or ``None`` when none exists.

    Prefers a system ffmpeg on ``PATH`` (typically a full build with libx264),
    then falls back to Playwright's bundled binaries. Each candidate is verified
    with :func:`_ffmpeg_supports_mp4`, so a WebM-only build is rejected rather
    than silently accepted.
    """
    system = shutil.which("ffmpeg")
    if system and _ffmpeg_supports_mp4(system):
        return system
    for candidate in _bundled_ffmpeg_candidates():
        if _ffmpeg_supports_mp4(str(candidate)):
            return str(candidate)
    return None


def _is_valid_mp4(path: Path) -> bool:
    """Report whether ``path`` begins with an ISO base-media (MP4) ``ftyp`` box.

    A genuine MP4 carries an ``ftyp`` box at the start, with the box type in
    bytes 4-8. WebM begins with the EBML signature instead, so this check
    distinguishes a real transcode from mislabeled WebM bytes.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    return len(header) >= 8 and header[4:8] == b"ftyp"


# ---------------------------------------------------------------------------
# Browser automation and screen recording (Playwright, lazy import)
# ---------------------------------------------------------------------------
class BrowserRecorder:
    """Drive a Playwright Chromium browser and record the ``/self-play`` screen.

    Playwright records WebM into a temporary directory; :meth:`stop_and_save`
    finalizes that recording into the exact ``.mp4`` target by transcoding with
    an MP4-capable ffmpeg, and raises :class:`RecordingFinalizationError` when no
    such ffmpeg is available rather than emitting a mislabeled file. The
    Playwright entrypoint and the ffmpeg resolver are injectable so tests can run
    without a real browser or ffmpeg.
    """

    def __init__(
        self,
        *,
        url: str,
        headless: bool = True,
        width: int = 1280,
        height: int = 720,
        playwright_factory: Callable[[], object] | None = None,
        ready_timeout_ms: int = 5000,
        ffmpeg_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        """Store recorder configuration; no browser is launched until :meth:`start`.

        Args:
            url: The ``/self-play`` URL to open.
            headless: Whether Chromium runs headless.
            width: Viewport and recording width in pixels.
            height: Viewport and recording height in pixels.
            playwright_factory: Optional zero-arg factory returning a Playwright
                context manager; resolved lazily to ``async_playwright`` when
                ``None``.
            ready_timeout_ms: Milliseconds to wait for the readiness hook.
            ffmpeg_resolver: Optional zero-arg callable returning the path to an
                MP4-capable ffmpeg, or ``None`` when none is available; defaults
                to :func:`_discover_mp4_capable_ffmpeg`.
        """
        self.url = url
        self.headless = headless
        self.width = width
        self.height = height
        self._playwright_factory = playwright_factory
        self.ready_timeout_ms = ready_timeout_ms
        self._ffmpeg_resolver = ffmpeg_resolver or _discover_mp4_capable_ffmpeg

        self._pw_context: object | None = None
        self._pw: object | None = None
        self._browser: object | None = None
        self._context: object | None = None
        self._page: object | None = None
        self._record_dir: str | None = None

    async def start(self) -> None:
        """Launch Chromium, open a recording context, and navigate to the URL.

        Raises:
            RuntimeError: If Playwright is not installed and no factory was
                injected.
        """
        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is required for self-play; run `make init` which "
                    "installs it and runs `playwright install chromium`"
                ) from exc
            factory = async_playwright

        self._pw_context = factory()
        self._pw = await self._pw_context.start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)

        self._record_dir = tempfile.mkdtemp(prefix="blitzy_selfplay_")
        size = {"width": self.width, "height": self.height}
        self._context = await self._browser.new_context(
            record_video_dir=self._record_dir,
            record_video_size=size,
            viewport=size,
        )
        self._page = await self._context.new_page()
        await self._page.goto(self.url, wait_until="domcontentloaded")

        try:
            await self._page.wait_for_function(_READY_HOOK_JS, timeout=self.ready_timeout_ms)
        except Exception:
            logger.warning(
                "self-play UI hook not found; recording will proceed without live "
                "board rendering - build the frontend with `make build` for full UI"
            )

    async def render(self, state: dict) -> None:
        """Push one move ``state`` into the browser via the render hook.

        A missing page or a failing hook is logged and swallowed so a render
        problem never aborts the game.
        """
        if self._page is None:
            return
        try:
            await self._page.evaluate(_RENDER_HOOK_JS, state)
        except Exception as exc:
            logger.debug("render hook failed: %s", exc)

    async def stop_and_save(self, target_mp4: Path) -> Path:
        """Close the browser, finalize the recording, and return the saved path.

        The video path is captured before closing the context (Playwright writes
        the file only on ``context.close()``). Every browser-teardown step is
        guarded so a recording problem cannot block server shutdown; the final
        MP4 conversion is not guarded here, so a finalization failure surfaces to
        the caller instead of leaving a mislabeled artifact.

        Args:
            target_mp4: The exact ``.mp4`` path the recording is saved to.

        Returns:
            The produced ``.mp4`` path.

        Raises:
            RecordingFinalizationError: If no WebM was recorded or no MP4-capable
                ffmpeg could produce a valid MP4 at ``target_mp4``.
        """
        webm_path_str: str | None = None
        if self._page is not None:
            with contextlib.suppress(Exception):
                video = self._page.video
                if video is not None:
                    webm_path_str = await video.path()

        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()

        return self._finalize_recording(webm_path_str, Path(target_mp4))

    def _finalize_recording(self, webm_path: str | None, target_mp4: Path) -> Path:
        """Transcode the recorded WebM into the exact ``.mp4`` artifact.

        Resolves an MP4-capable ffmpeg, transcodes the WebM to H.264/MP4, and
        verifies the output is a real MP4 before returning it. A partial or
        invalid output is removed. The WebM bytes are never copied under the
        ``.mp4`` name.

        Args:
            webm_path: Path to the WebM Playwright recorded, or ``None``.
            target_mp4: The exact ``.mp4`` path to produce.

        Returns:
            The produced, validated ``.mp4`` path.

        Raises:
            RecordingFinalizationError: If there is no WebM source, no MP4-capable
                ffmpeg, the transcode fails, or the output is not a valid MP4.
        """
        target_mp4 = Path(target_mp4)
        target_mp4.parent.mkdir(parents=True, exist_ok=True)

        if not webm_path or not Path(webm_path).exists():
            raise RecordingFinalizationError(
                "no WebM recording was produced by the browser; cannot create the "
                f"required MP4 artifact at {target_mp4}"
            )

        ffmpeg = self._ffmpeg_resolver()
        if not ffmpeg:
            raise RecordingFinalizationError(
                "no MP4-capable ffmpeg was found (a system ffmpeg with an MP4 muxer "
                "is required; Playwright's bundled ffmpeg only muxes WebM). Install "
                "ffmpeg and ensure it is on PATH, then re-run `make self-play`. The "
                f"WebM recording was left at {webm_path}"
            )

        try:
            completed = _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(webm_path),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-an",
                    str(target_mp4),
                ]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._remove_partial(target_mp4)
            raise RecordingFinalizationError(
                f"ffmpeg invocation failed while transcoding to {target_mp4}: {exc}"
            ) from exc

        if completed.returncode != 0:
            self._remove_partial(target_mp4)
            raise RecordingFinalizationError(
                f"ffmpeg transcode to MP4 failed (exit code {completed.returncode}): "
                f"{(completed.stderr or '').strip()[-500:]}"
            )

        if not target_mp4.exists() or not _is_valid_mp4(target_mp4):
            self._remove_partial(target_mp4)
            raise RecordingFinalizationError(f"ffmpeg did not produce a valid MP4 at {target_mp4}")

        return target_mp4

    @staticmethod
    def _remove_partial(path: Path) -> None:
        """Delete a partial or invalid output file, ignoring missing-file errors."""
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    async def cleanup(self) -> None:
        """Close any open handles and remove the temporary recording directory.

        Idempotent and fully guarded; safe to call after :meth:`stop_and_save`.
        """
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None
        self._pw_context = None
        self._page = None

        if self._record_dir:
            shutil.rmtree(self._record_dir, ignore_errors=True)
            self._record_dir = None


# ---------------------------------------------------------------------------
# Default seams for the pure game-driver
# ---------------------------------------------------------------------------
async def _noop_render(state: dict) -> None:
    """Default render callback that does nothing."""
    return None


def _default_search(
    searcher: Searcher, board: chess.Board, limits: SearchLimits
) -> Awaitable[SearchResult]:
    """Offload a single synchronous search onto a worker thread.

    Returns the ``asyncio.to_thread`` awaitable so the caller stays off the
    event loop during the CPU-bound search (Constraint 2).
    """
    return asyncio.to_thread(searcher.search, board, limits)


def _result_reason(board: chess.Board, ply: int, max_plies: int) -> str:
    """Classify why the game ended into a short human-readable reason."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    if board.is_seventyfive_moves():
        return "seventy-five-move rule"
    if board.is_fivefold_repetition():
        return "fivefold repetition"
    if board.can_claim_fifty_moves():
        return "fifty-move rule"
    if board.can_claim_threefold_repetition():
        return "threefold repetition"
    if ply >= max_plies:
        return "move limit reached"
    return "game incomplete"


# ---------------------------------------------------------------------------
# Pure async game-driver (the unit-testable heart)
# ---------------------------------------------------------------------------
async def play_self_play_game(
    *,
    white_searcher: Searcher,
    black_searcher: Searcher,
    evaluator: Evaluator,
    board: chess.Board | None = None,
    render: RenderCallable | None = None,
    sleep: SleepCallable = asyncio.sleep,
    search: SearchCallable | None = None,
    move_delay_ms: int = config.SELF_PLAY_MOVE_DELAY_MS,
    max_plies: int = 200,
    game_start: float | None = None,
) -> tuple[list[MoveAnnotation], str, str]:
    """Play a Hard-versus-Medium self-play game and collect annotations.

    Hard plays White and Medium plays Black; both tiers are looked up from
    ``config.DIFFICULTY_TIERS``. Every engine call is offloaded through the
    injected ``search`` callable (which always uses ``asyncio.to_thread``) on a
    ``board.copy()``, and each move occupies at least ``move_delay_ms`` of
    wall-clock time so it renders visibly. All I/O is injected, so this function
    has no Playwright or subprocess dependency.

    Args:
        white_searcher: Searcher for White (Hard tier).
        black_searcher: Searcher for Black (Medium tier).
        evaluator: Evaluator used for the per-position component breakdown.
        board: Starting position; a fresh ``chess.Board`` when ``None``.
        render: Async per-move render callback; a no-op when ``None``.
        sleep: Async sleep used for pacing; ``asyncio.sleep`` by default.
        search: Async single-search offloader; :func:`_default_search` when
            ``None``.
        move_delay_ms: Minimum wall-clock milliseconds per move.
        max_plies: Half-move ceiling that force-stops a non-terminating game.
        game_start: Monotonic reference for elapsed timestamps; captured at
            entry when ``None``.

    Returns:
        A tuple ``(annotations, result_str, result_reason)`` where
        ``result_str`` is the python-chess result string and ``result_reason``
        explains the ending.
    """
    board = board if board is not None else chess.Board()
    if render is None:
        render = _noop_render
    if search is None:
        search = _default_search
    if game_start is None:
        game_start = time.monotonic()

    annotations: list[MoveAnnotation] = []
    hard_tier = config.DIFFICULTY_TIERS["hard"]
    medium_tier = config.DIFFICULTY_TIERS["medium"]

    ply = 0
    while not board.is_game_over(claim_draw=True) and ply < max_plies:
        ply += 1
        is_white = board.turn == chess.WHITE
        searcher = white_searcher if is_white else black_searcher
        tier = hard_tier if is_white else medium_tier
        tier_name = "Hard" if is_white else "Medium"
        color = "White" if is_white else "Black"
        limits = SearchLimits.from_tier(tier)

        t0 = time.monotonic()
        result = await search(searcher, board.copy(), limits)

        move = result.best_move
        if move is None:
            logger.info("no move returned at ply %d; ending game", ply)
            break
        if not board.is_legal(move):
            logger.warning("engine returned illegal move %s at ply %d; ending game", move, ply)
            break

        san = board.san(move)
        uci = move.uci()
        is_capture = board.is_capture(move)
        is_check = board.gives_check(move)

        alternatives: list[tuple[str, int]] = []
        for entry in result.ranked_moves[:3]:
            if not entry:
                continue
            alt_move, alt_score = entry
            if alt_move is None or not board.is_legal(alt_move):
                continue
            alternatives.append((board.san(alt_move), int(alt_score)))

        score_cp_white = result.score_cp if is_white else -result.score_cp

        fen_before = board.fen()
        move_number = (ply + 1) // 2
        board.push(move)
        fen_after = board.fen()
        is_checkmate = board.is_checkmate()

        components_obj = await asyncio.to_thread(evaluator.evaluate_components, board.copy())
        components = {
            "material": int(components_obj.material),
            "positional": int(components_obj.positional),
            "pawns": int(components_obj.pawns),
            "king_safety": int(components_obj.king_safety),
            "mobility": int(components_obj.mobility),
            "total": int(components_obj.total),
        }
        phase = getattr(components_obj, "phase", None)

        elapsed_s = time.monotonic() - game_start

        annotations.append(
            MoveAnnotation(
                ply=ply,
                move_number=move_number,
                color=color,
                tier=tier_name,
                san=san,
                uci=uci,
                fen_before=fen_before,
                fen_after=fen_after,
                elapsed_s=elapsed_s,
                score_cp_white=int(score_cp_white),
                components=components,
                phase=phase,
                alternatives=alternatives,
                is_capture=is_capture,
                is_check=is_check,
                is_checkmate=is_checkmate,
                from_book=result.from_book,
                from_tablebase=result.from_tablebase,
                nodes=result.nodes,
                depth=result.depth,
            )
        )

        if is_checkmate:
            status = "checkmate"
        elif board.is_check():
            status = "check"
        elif board.is_game_over(claim_draw=True):
            status = "gameover"
        else:
            status = "playing"

        side_to_move = "white" if board.turn == chess.WHITE else "black"
        state = {
            "fen": fen_after,
            "lastMove": {"from": uci[:2], "to": uci[2:4], "uci": uci},
            "moveNumber": move_number,
            "sideToMove": side_to_move,
            "san": san,
            "whiteTier": "Hard",
            "blackTier": "Medium",
            "evalCp": int(score_cp_white),
            "status": status,
        }
        await render(state)

        elapsed_move = time.monotonic() - t0
        remaining = (move_delay_ms / 1000.0) - elapsed_move
        if remaining > 0:
            await sleep(remaining)

    result_str = board.result(claim_draw=True)
    result_reason = _result_reason(board, ply, max_plies)
    return annotations, result_str, result_reason


# ---------------------------------------------------------------------------
# Full pipeline orchestrator
# ---------------------------------------------------------------------------
async def run_self_play(
    *,
    now: datetime | None = None,
    server: ServerProcess | None = None,
    recorder: BrowserRecorder | None = None,
    headless: bool = True,
    max_plies: int = 200,
) -> dict:
    """Run the complete self-play demonstration and return a summary.

    The single ``now`` timestamp is shared by the recording path and the
    transcript path so their stems match. The server and recorder are torn down
    in a ``finally`` block, and the transcript is written there too, so shutdown
    and the transcript run even when the game raises (the error then propagates
    to the caller).

    Args:
        now: Timestamp for the artifact names; current time when ``None``.
        server: Injectable server lifecycle; a default :class:`ServerProcess`
            when ``None``.
        recorder: Injectable browser recorder; a default :class:`BrowserRecorder`
            when ``None``.
        headless: Whether the default recorder runs headless.
        max_plies: Half-move ceiling passed through to the game driver.

    Returns:
        A summary dict with ``recording``, ``transcript``, ``result``,
        ``reason``, and ``moves`` keys.
    """
    now = now or datetime.now()
    recording_path = config.self_play_recording_path(now)
    transcript_path = transcript_path_for(recording_path)
    config.ensure_dirs()

    book = await asyncio.to_thread(load_book)
    tablebase = await asyncio.to_thread(open_tablebase)
    evaluator = Evaluator()
    white_searcher = Searcher(book=book, tablebase=tablebase)
    black_searcher = Searcher(book=book, tablebase=tablebase)

    server = server or ServerProcess()
    url = f"http://127.0.0.1:{config.BACKEND_PORT}/self-play"
    recorder = recorder or BrowserRecorder(url=url, headless=headless)

    annotations: list[MoveAnnotation] = []
    result_str = "*"
    result_reason = "game incomplete"
    finalize_error: Exception | None = None

    try:
        await server.start()
        await recorder.start()
        game_start = time.monotonic()
        annotations, result_str, result_reason = await play_self_play_game(
            white_searcher=white_searcher,
            black_searcher=black_searcher,
            evaluator=evaluator,
            render=recorder.render,
            max_plies=max_plies,
            game_start=game_start,
        )
    finally:
        # The recording is finalized first but its failure is captured rather
        # than raised here, so cleanup, server shutdown, and the transcript still
        # run; a captured failure is re-raised after teardown (see below).
        try:
            produced = await recorder.stop_and_save(recording_path)
            logger.info("recording finalized: %s", produced)
        except Exception as exc:
            finalize_error = exc
            logger.error("recording finalization failed: %s", exc)
        with contextlib.suppress(Exception):
            await recorder.cleanup()
        with contextlib.suppress(Exception):
            await server.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                write_transcript,
                annotations,
                transcript_path,
                white_tier="Hard",
                black_tier="Medium",
                recording_filename=recording_path.name,
                result=result_str,
                result_reason=result_reason,
                generated_at=now,
            )

    # Reached only when the game body did not raise. A finalization failure
    # captured during teardown is re-raised here so it propagates to the caller.
    if finalize_error is not None:
        raise finalize_error

    summary = {
        "recording": recording_path,
        "transcript": transcript_path,
        "result": result_str,
        "reason": result_reason,
        "moves": len(annotations),
    }
    logger.info(
        "self-play complete: result=%s (%s), moves=%d, recording=%s",
        result_str,
        result_reason,
        len(annotations),
        recording_path,
    )
    return summary


# ---------------------------------------------------------------------------
# Command-line entry point (`python -m chess_ai.self_play.runner`)
# ---------------------------------------------------------------------------
async def main(argv: list[str] | None = None) -> int:
    """Parse CLI flags, run the demonstration, and report the artifact paths.

    Args:
        argv: Optional argument list (defaults to ``sys.argv`` via argparse).

    Returns:
        ``0`` on success, ``1`` on an unhandled error.
    """
    parser = argparse.ArgumentParser(
        prog="chess_ai.self_play.runner",
        description="Run the AI self-play demonstration with screen recording.",
    )
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run the browser headless (default).",
    )
    headless_group.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Run the browser with a visible window.",
    )
    parser.set_defaults(headless=True)
    parser.add_argument(
        "--max-plies",
        type=int,
        default=200,
        help="Maximum half-moves before the game is force-stopped (default: 200).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    try:
        summary = await run_self_play(headless=args.headless, max_plies=args.max_plies)
    except Exception:
        logger.exception("self-play run failed")
        return 1

    print(f"Recording:  {summary['recording']}")
    print(f"Transcript: {summary['transcript']}")
    print(f"Result:     {summary['result']} ({summary['reason']}), moves: {summary['moves']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
