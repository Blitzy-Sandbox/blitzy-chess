"""Self-play demonstration subpackage for the blitzy-chess backend.

Contains the orchestration runner (``runner``, driven by ``make self-play``),
which drives the browser through Playwright and offloads the engine search, and
the commentary transcript writer (``annotator``), which is standard-library
only.

This package root is intentionally side-effect-free: it imports none of its
submodules. ``runner`` and its Playwright dependency are not imported at
package-import time; each submodule is imported explicitly where it is used.
"""
