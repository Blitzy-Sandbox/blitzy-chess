#!/usr/bin/env python3
"""Download Syzygy endgame tablebases used by the chess AI.

Backs ``make download-syzygy``. The Makefile runs this with the virtual-environment
Python from the repository root::

    python backend/scripts/download_syzygy.py

Files are written flat into ``backend/tables/`` (the directory the engine's
``endgame.py`` opens via ``chess.syzygy.open_tablebase``); that module scans the
directory for ``*.rtbw`` (WDL) and ``*.rtbz`` (DTZ) tables and probes positions
with six or fewer pieces. By default this fetches the 3-4-5 piece set - the
"essential" tables - from the public Lichess tablebase server.

The download is:

* idempotent  - tables already present are skipped unless ``--force`` is given,
                so an interrupted run is resumed simply by re-running it;
* resilient   - a clear, actionable message is printed and the process exits
                non-zero on a hard error so ``make`` surfaces the failure;
* atomic      - each file lands in a temporary file and is renamed into place
                only after a complete download;
* dependency-light - standard library only (``urllib``); the Syzygy data format
                itself is handled by the engine, not by this fetcher.

The full 3-4-5 set is roughly 1 GiB. Use ``--minimal`` for a tiny 3-piece subset,
``--wdl-only`` to halve the size, or ``--max-files N`` to cap a quick download.

Override the source or destination without editing this file::

    SYZYGY_BASE_URL=<url> SYZYGY_DIR=<path> python backend/scripts/download_syzygy.py
    python backend/scripts/download_syzygy.py --dest <path> --minimal --force
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Public Lichess Syzygy server. The 3-4-5 piece WDL/DTZ tables live under these
# subpaths; point SYZYGY_BASE_URL / --base-url at a mirror to change the source.
DEFAULT_BASE_URL = "https://tablebase.lichess.ovh/tables/standard/"
CATEGORY_SUBDIRS = {
    "wdl": ("3-4-5-wdl/", ".rtbw"),
    "dtz": ("3-4-5-dtz/", ".rtbz"),
}
# Canonical 3-piece table names - guaranteed present on the server. Used for the
# --minimal subset and as a fallback when the directory index cannot be parsed.
MINIMAL_TABLES = ("KQvK", "KRvK", "KBvK", "KNvK", "KPvK")
USER_AGENT = "blitzy-chess-syzygy-fetch/1.0"
DEFAULT_TIMEOUT_S = 60
_HREF_RE = re.compile(r'href=["\']?([^"\'>?]+\.rtb[wz])["\'> ]', re.IGNORECASE)


def resolve_default_dest() -> Path:
    """Return the default tables directory from ``chess_ai.config`` with a stdlib fallback.

    The script may run from any working directory (the Makefile runs it from the
    repository root), so the backend root is derived from this file's location and
    inserted on ``sys.path`` to import the stdlib-only config module. If that
    import fails, an identical path is computed directly, because ``config.py``
    resolves the same ``__file__``-relative layout.
    """
    backend_root = Path(__file__).resolve().parent.parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    try:
        from chess_ai import config  # type: ignore import-not-found

        return Path(config.TABLES_DIR)
    except Exception:
        return backend_root / "tables"


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def _normalize_base(base_url: str) -> str:
    return base_url if base_url.endswith("/") else base_url + "/"


def _open(url: str, timeout: int):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def list_remote_tables(base_url: str, subdir: str, extension: str, *, timeout: int) -> list[str]:
    """Return table file names under ``base_url + subdir`` by parsing its index page.

    Returns an empty list (rather than raising) if the index cannot be fetched or
    parsed, so callers can fall back to the built-in minimal set.
    """
    index_url = _normalize_base(base_url) + subdir
    try:
        with _open(index_url, timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    names = {
        os.path.basename(match)
        for match in _HREF_RE.findall(html)
        if match.lower().endswith(extension)
    }
    return sorted(names)


def download_file(url: str, dest: Path, *, timeout: int) -> int:
    """Download ``url`` to ``dest`` atomically. Return the byte count.

    Raises ``RuntimeError`` with an actionable message on any failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with _open(url, timeout) as response, os.fdopen(tmp_fd, "wb") as tmp_file:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                tmp_file.write(chunk)
                total += len(chunk)
        if total == 0:
            raise RuntimeError("the download produced an empty file")
        os.replace(tmp_path, dest)
        return total
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} ({exc.reason})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    except (socket.timeout, TimeoutError) as exc:  # noqa: UP041
        raise RuntimeError(f"timed out after {timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"write error: {exc}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def selected_categories(args: argparse.Namespace) -> list[str]:
    if args.wdl_only:
        return ["wdl"]
    if args.dtz_only:
        return ["dtz"]
    return ["wdl", "dtz"]


def build_download_plan(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return a list of (url, filename) pairs to fetch, honoring the chosen options."""
    base = _normalize_base(args.base_url)
    plan: list[tuple[str, str]] = []
    for category in selected_categories(args):
        subdir, extension = CATEGORY_SUBDIRS[category]
        if args.minimal:
            names = [name + extension for name in MINIMAL_TABLES]
        else:
            names = list_remote_tables(args.base_url, subdir, extension, timeout=args.timeout)
            if not names:
                print(
                    f"  warning: could not read the index at {base + subdir}; "
                    "falling back to the minimal 3-piece set.",
                    file=sys.stderr,
                )
                names = [name + extension for name in MINIMAL_TABLES]
        for name in names:
            plan.append((base + subdir + name, name))
    if args.max_files is not None:
        plan = plan[: args.max_files]
    return plan


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Syzygy endgame tablebases into backend/tables/.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SYZYGY_BASE_URL", DEFAULT_BASE_URL),
        help="base URL of the Syzygy server (env: SYZYGY_BASE_URL).",
    )
    parser.add_argument(
        "--dest",
        "--output",
        dest="dest",
        default=None,
        help="destination directory (default: chess_ai.config.TABLES_DIR).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--wdl-only", action="store_true", help="download only WDL (.rtbw) tables.")
    group.add_argument("--dtz-only", action="store_true", help="download only DTZ (.rtbz) tables.")
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="download only the small 3-piece subset (KQvK, KRvK, KBvK, KNvK, KPvK).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="cap the number of files downloaded (useful for a quick subset).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download tables even if they already exist.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("SYZYGY_TIMEOUT", DEFAULT_TIMEOUT_S)),
        help=f"per-file network timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dest_dir = Path(args.dest).expanduser() if args.dest else resolve_default_dest()
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        print(
            f"Resolving Syzygy tablebases\n"
            f"  from {_normalize_base(args.base_url)}\n"
            f"  to   {dest_dir}"
        )

    try:
        plan = build_download_plan(args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # defensive: planning must not crash make
        print(f"error: could not build the download list: {exc}", file=sys.stderr)
        return 1

    if not plan:
        print("error: no tables to download (empty plan).", file=sys.stderr)
        print(
            "       Check --base-url / SYZYGY_BASE_URL, or pass --minimal for the 3-piece set.",
            file=sys.stderr,
        )
        return 1

    downloaded = skipped = failed = 0
    total_bytes = 0
    for index, (url, name) in enumerate(plan, start=1):
        dest = dest_dir / name
        if dest.exists() and dest.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        try:
            size = download_file(url, dest, timeout=args.timeout)
            downloaded += 1
            total_bytes += size
            if not args.quiet:
                print(f"  [{index}/{len(plan)}] {name}  ({_human_size(size)})")
        except RuntimeError as exc:
            failed += 1
            print(f"  [{index}/{len(plan)}] {name}  FAILED: {exc}", file=sys.stderr)

    if not args.quiet:
        print(
            f"Done: {downloaded} downloaded ({_human_size(total_bytes)}), "
            f"{skipped} already present, {failed} failed."
        )

    if downloaded == 0 and skipped == 0:
        print(
            "error: no Syzygy tables could be downloaded. Check your network connection "
            "or --base-url, then re-run `make download-syzygy`.",
            file=sys.stderr,
        )
        return 1
    if failed:
        print(
            f"error: {failed} table(s) failed to download; re-run `make download-syzygy` "
            "to resume (existing files are skipped).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
