#!/usr/bin/env python3
"""Download the Polyglot opening book used by the chess AI.

Backs ``make init``. The Makefile runs this with the virtual-environment Python
from the repository root::

    python backend/scripts/download_book.py

The book is written to ``backend/books/opening_book.bin`` (the path the engine's
``book.py`` opens via ``chess.polyglot.open_reader``). The download is:

* idempotent  - an existing book is left in place unless ``--force`` is given;
* resilient   - a clear, actionable message is printed and the process exits
                non-zero on any hard error so ``make`` surfaces the failure;
* atomic      - bytes land in a temporary file and are renamed into place only
                after a complete, non-empty download;
* dependency-light - standard library only (``urllib``); the chess data format
                itself is handled by the engine, not by this fetcher.

Override the source or destination without editing this file::

    OPENING_BOOK_URL=<url> python backend/scripts/download_book.py
    python backend/scripts/download_book.py --url <url> --output <path> --force
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Default source: a valid Polyglot book shipped in the python-chess repository.
# Override via OPENING_BOOK_URL / --url to use a different or fuller book.
DEFAULT_BOOK_URL = (
    "https://github.com/niklasf/python-chess/raw/master/data/polyglot/performance.bin"
)
USER_AGENT = "blitzy-chess-opening-book-fetch/1.0"
DEFAULT_TIMEOUT_S = 30
_POLYGLOT_ENTRY_BYTES = 16  # every Polyglot record is exactly 16 bytes


def resolve_default_output() -> Path:
    """Return the default book path from ``chess_ai.config`` with a stdlib fallback.

    The script may be invoked with any working directory (the Makefile runs it
    from the repository root), so the backend root is derived from this file's
    location and inserted on ``sys.path`` to import the stdlib-only config
    module. If that import fails for any reason, an identical path is computed
    directly, because ``config.py`` resolves the same ``__file__``-relative
    layout.
    """
    backend_root = Path(__file__).resolve().parent.parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    try:
        from chess_ai import config  # type: ignore import-not-found

        return Path(config.OPENING_BOOK_PATH)
    except Exception:
        return backend_root / "books" / "opening_book.bin"


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def download_file(url: str, dest: Path, *, timeout: int, quiet: bool) -> int:
    """Download ``url`` to ``dest`` atomically. Return the byte count.

    Raises ``RuntimeError`` with an actionable message on any failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    tmp_fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with os.fdopen(tmp_fd, "wb") as tmp_file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                    total += len(chunk)
        if total == 0:
            raise RuntimeError("the download produced an empty file")
        if total % _POLYGLOT_ENTRY_BYTES != 0 and not quiet:
            print(
                f"  warning: {total} bytes is not a multiple of {_POLYGLOT_ENTRY_BYTES}; "
                "this may not be a valid Polyglot book.",
                file=sys.stderr,
            )
        os.replace(tmp_path, dest)
        return total
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"server returned HTTP {exc.code} ({exc.reason}) for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach {url}: {exc.reason}") from exc
    except (socket.timeout, TimeoutError) as exc:  # noqa: UP041
        raise RuntimeError(f"timed out after {timeout}s downloading {url}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not write {dest}: {exc}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the Polyglot opening book into backend/books/.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OPENING_BOOK_URL", DEFAULT_BOOK_URL),
        help="source URL of the Polyglot .bin book (env: OPENING_BOOK_URL).",
    )
    parser.add_argument(
        "--output",
        "--dest",
        dest="output",
        default=None,
        help="destination path (default: chess_ai.config.OPENING_BOOK_PATH).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if the book already exists.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("OPENING_BOOK_TIMEOUT", DEFAULT_TIMEOUT_S)),
        help=f"network timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dest = Path(args.output).expanduser() if args.output else resolve_default_output()

    if dest.exists() and dest.stat().st_size > 0 and not args.force:
        if not args.quiet:
            print(f"Opening book already present at {dest} ({_human_size(dest.stat().st_size)}).")
            print("Nothing to do (use --force to re-download).")
        return 0

    if not args.quiet:
        print(f"Downloading opening book\n  from {args.url}\n  to   {dest}")
    try:
        size = download_file(args.url, dest, timeout=args.timeout, quiet=args.quiet)
    except RuntimeError as exc:
        print(f"error: failed to download the opening book: {exc}", file=sys.stderr)
        print(
            "       Check your network connection, or set OPENING_BOOK_URL to a reachable "
            "Polyglot .bin and re-run `make init`.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"Done: wrote {_human_size(size)} to {dest}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
