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
* verified    - the source URL is pinned to an immutable release tag, and an
                expected SHA-256 (``--sha256`` / ``OPENING_BOOK_SHA256``) is
                checked before the file is installed; a custom ``--url`` must be
                accompanied by a digest or an explicit ``--allow-unverified``;
* dependency-light - standard library only (``urllib`` + ``hashlib``); the chess
                data format itself is handled by the engine, not by this fetcher.

Override the source or destination without editing this file::

    OPENING_BOOK_URL=<url> python backend/scripts/download_book.py
    python backend/scripts/download_book.py --url <url> --sha256 <hex> --output <path> --force
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Default source: a valid Polyglot book shipped in the python-chess repository,
# pinned to an IMMUTABLE release tag (not a moving branch) so the fetched artifact
# is reproducible. Override via OPENING_BOOK_URL / --url to use a different book.
DEFAULT_BOOK_URL = (
    "https://github.com/niklasf/python-chess/raw/v1.11.2/data/polyglot/performance.bin"
)
# Expected SHA-256 of the default book. Left as None because the digest of the
# pinned artifact has not been verified against the immutable source from inside
# this build, and pinning an unverified hash would reject the legitimate file.
# The immutable tag above is the reproducibility anchor; supply a digest via
# --sha256 / OPENING_BOOK_SHA256 to enforce verification. The script prints the
# computed digest on every download so a maintainer can record it here later.
_DEFAULT_BOOK_SHA256: str | None = None
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


def download_file(
    url: str,
    dest: Path,
    *,
    timeout: int,
    quiet: bool,
    expected_sha256: str | None = None,
) -> int:
    """Download ``url`` to ``dest`` atomically, verifying integrity. Return byte count.

    The bytes stream to a temporary file while a SHA-256 digest is computed. When
    ``expected_sha256`` is given, the digest MUST match before the file is moved
    into place; a mismatch fails closed (the destination is left untouched). When
    it is ``None`` the computed digest is reported (unless ``quiet``) so it can be
    pinned. Directory and temp-file creation run inside the handled block, so
    filesystem failures surface as an actionable ``RuntimeError`` -- never a raw
    traceback -- and any partial temp file is cleaned up.

    Raises ``RuntimeError`` with an actionable message on any failure.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
        tmp_path = Path(tmp_name)
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with os.fdopen(tmp_fd, "wb") as tmp_file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
        if total == 0:
            raise RuntimeError("the download produced an empty file")
        if total % _POLYGLOT_ENTRY_BYTES != 0 and not quiet:
            print(
                f"  warning: {total} bytes is not a multiple of {_POLYGLOT_ENTRY_BYTES}; "
                "this may not be a valid Polyglot book.",
                file=sys.stderr,
            )
        actual_sha256 = digest.hexdigest()
        # Integrity gate: verify BEFORE the artifact is moved into place so a
        # corrupt or tampered download can never replace the installed book.
        if expected_sha256 is not None:
            if actual_sha256.lower() != expected_sha256.strip().lower():
                raise RuntimeError(
                    f"checksum mismatch: expected SHA-256 {expected_sha256.strip().lower()} "
                    f"but the downloaded data hashes to {actual_sha256}; the book was NOT "
                    "installed. Verify the source or update the expected digest."
                )
            if not quiet:
                print(f"  verified SHA-256 {actual_sha256}")
        elif not quiet:
            print(f"  computed SHA-256 {actual_sha256} (unverified; pass --sha256 to enforce)")
        os.replace(tmp_path, dest)
        return total
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"server returned HTTP {exc.code} ({exc.reason}) for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach {url}: {exc.reason}") from exc
    except (socket.timeout, TimeoutError) as exc:  # noqa: UP041
        raise RuntimeError(f"timed out after {timeout}s downloading {url}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not write to {dest.parent}: {exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
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
    parser.add_argument(
        "--sha256",
        default=None,
        help="expected SHA-256 of the book, verified before install (env: OPENING_BOOK_SHA256).",
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="permit a custom --url with no --sha256 digest (bypasses the integrity check).",
    )
    return parser.parse_args(argv)


def resolve_expected_sha256(args: argparse.Namespace) -> str | None:
    """Return the SHA-256 to verify against, or raise if a custom URL is unverified.

    Precedence: an explicit ``--sha256`` / ``OPENING_BOOK_SHA256`` always wins.
    Otherwise the default (immutably pinned) source uses ``_DEFAULT_BOOK_SHA256``
    (which may be ``None`` -- "report but do not enforce"). A CUSTOM ``--url`` with
    no digest is refused unless ``--allow-unverified`` is set, so an untrusted
    source can never be installed silently.

    Raises:
        RuntimeError: If a custom URL is requested without a digest and without
            an explicit ``--allow-unverified`` override.
    """
    explicit = args.sha256 or os.environ.get("OPENING_BOOK_SHA256")
    if explicit:
        return explicit.strip()
    if args.url == DEFAULT_BOOK_URL:
        return _DEFAULT_BOOK_SHA256
    if not args.allow_unverified:
        raise RuntimeError(
            "refusing to download from a custom --url without integrity verification; "
            "pass --sha256 <hexdigest> (or set OPENING_BOOK_SHA256), or --allow-unverified "
            "to bypass this check explicitly."
        )
    return None


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
        expected_sha256 = resolve_expected_sha256(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        size = download_file(
            args.url,
            dest,
            timeout=args.timeout,
            quiet=args.quiet,
            expected_sha256=expected_sha256,
        )
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
