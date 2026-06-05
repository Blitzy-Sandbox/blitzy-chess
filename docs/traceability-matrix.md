# Traceability Matrix

This matrix maps every requirement in `blitzy-chess` to the file or files that implement it, so a reviewer can verify coverage at a glance. The first table lists the prompt's 17 verification constraints, numbered to match AAP §0.1.2; the second lists the five project-level rules. A short key-parameters table follows, so reviewers can spot-check the exact values and formulas the engine uses.

## The 17 verification constraints

Each row maps a constraint from AAP §0.1.2 to its implementing file(s).

| # | Constraint (summary) | Implementing file(s) | Evidence / Notes |
|---|----------------------|----------------------|------------------|
| 1 | All chess rules enforced by python-chess on the backend; client chess.js is display-only; server is authoritative | `backend/chess_ai/api/game_ws.py`, `backend/chess_ai/api/multiplayer_ws.py`, `frontend/src/hooks/useGameState.ts` | Server validates and derives state with python-chess; the chess.js mirror is display and SAN only |
| 2 | AI search must not block the FastAPI event loop; offload via `asyncio.to_thread()`; search stays synchronous | `backend/chess_ai/api/game_ws.py`, `backend/chess_ai/self_play/runner.py`, `backend/chess_ai/engine/search.py` | Handlers call the synchronous search through `asyncio.to_thread()` |
| 3 | `engine/` package contains zero FastAPI/Starlette/WebSocket imports (pure computation) | `backend/chess_ai/engine/` (all modules), `backend/tests/test_evaluator.py`, `backend/tests/test_search.py` | Engine imports only `chess` and stdlib; tests import the engine without the web stack |
| 4 | Evaluation interpolates midgame/endgame piece-square tables by a material phase 0–24 | `backend/chess_ai/engine/evaluator.py`, `backend/chess_ai/engine/tables.py` | Phase computed from remaining material; piece-square tables interpolated |
| 5 | Pawn-structure evaluation cached in a pawn hash table keyed on a pawn-only Zobrist hash | `backend/chess_ai/engine/evaluator.py` | Pawn-only key distinct from the full-board key |
| 6 | Transposition table uses `board.zobrist_hash()` (64-bit int), not the internal `_transposition_key()` | `backend/chess_ai/engine/search.py` | Table keyed on the 64-bit Zobrist hash; capped at 256 MB / 2^20 entries |
| 7 | Move history shown as paired algebraic notation | `frontend/src/components/MoveHistory.tsx` | Renders numbered pairs (white, black) |
| 8 | Opening book probed before search every move; book moves still respect `MIN_AI_DELAY_MS = 1500` | `backend/chess_ai/engine/book.py`, `backend/chess_ai/api/game_ws.py`, `backend/chess_ai/config.py` | Book probe precedes search; pacing enforced from config |
| 9 | Every operation runs through the Makefile; README references only make targets | `Makefile`, `README.md` | All workflows are `make` targets; the README cites targets only |
| 10 | Late move reduction uses `R = max(1, floor(log(depth) * log(moveIndex) / 2.0))` | `backend/chess_ai/engine/search.py` | Late-move-reduction formula implemented verbatim |
| 11 | `test_search.py` holds ≥10 FEN tactical tests (3 mate-in-1, 2 mate-in-2, 2 hanging-piece, 2 passed-pawn, 1 stalemate-avoidance) | `backend/tests/test_search.py` | Tactical suite with the exact mix |
| 12 | WebSocket server validates every move with `board.is_legal()`; illegal moves rejected and tested | `backend/chess_ai/api/game_ws.py`, `backend/chess_ai/api/multiplayer_ws.py`, `backend/tests/test_game_ws.py`, `backend/tests/test_multiplayer_ws.py` | Server-side legality check; rejection paths covered by tests |
| 13 | Self-play transcript carries `[MM:SS]` timestamps, WHY commentary with eval components in centipawns, top-3 alternatives, and YouTube chapter markers | `backend/chess_ai/self_play/annotator.py` | Annotator emits the timestamped commentary format |
| 14 | Self-play renders visually in the browser at the full UI, ≥5s per move; runner orchestrates start/record/play/transcript/shutdown | `backend/chess_ai/self_play/runner.py`, `frontend/src/components/SelfPlayView.tsx`, `backend/chess_ai/config.py` | Pacing `SELF_PLAY_MOVE_DELAY_MS = 5000`; runner sequences the demo |
| 15 | Frontend renders the board only through react-chessboard (no custom canvas/SVG) | `frontend/src/components/GameBoard.tsx` | Single react-chessboard wrapper; no hand-rolled rendering |
| 16 | Frontend never uses HTTP REST for game moves (WebSocket only); REST limited to health and initial load | `frontend/src/hooks/useGameWebSocket.ts`, `frontend/src/hooks/useMultiplayerWebSocket.ts`, `backend/chess_ai/api/health.py` | Moves flow over WebSocket; REST is health and initial load |
| 17 | Vite dev server proxies `/ws/` and `/api/` to `localhost:8000` | `frontend/vite.config.ts` | Dev proxy configured for both prefixes |

## The five project-level rules

Each row maps a cross-cutting project rule to the file(s) that satisfy it.

| Project rule | Mandate (summary) | Implementing file(s) | Evidence / Notes |
|--------------|-------------------|----------------------|------------------|
| Observability | Structured logging with correlation IDs, distributed tracing, a `/metrics` endpoint, health and readiness checks, and a dashboard template, verified locally | `backend/chess_ai/observability/logging_config.py`, `backend/chess_ai/observability/tracing.py`, `backend/chess_ai/observability/metrics.py`, `backend/chess_ai/observability/dashboards/chess_ai_dashboard.json`, `backend/chess_ai/api/health.py`, `backend/chess_ai/app.py` | Logging, tracing, and metrics wired at the app edge; readiness lives in health; dashboard template shipped |
| Onboarding & Continued Development | Docs that take a developer from a clean machine to a running, modifiable app, with suggested next tasks | `README.md`, `docs/onboarding.md` | Clean-machine-to-running guide via make targets |
| Explainability | A decision log (Markdown table: decision, alternatives, rationale, risk) plus a traceability matrix; rationale in the log, not code comments | `docs/decision-log.md`, `docs/traceability-matrix.md` | This matrix and the decision log satisfy the rule |
| Executive Presentation | A self-contained reveal.js deck for leadership, on the Blitzy brand, with CDN-pinned reveal.js 5.1.0, Mermaid 11.4.0, Lucide 0.460.0 | `executive-summary.html`, `blitzy-deck/references/blitzy-reveal-theme.css` | Single-file deck plus the referenced theme |
| Prose | Generated prose validated for clarity and directness (Vonnegut/Asimov style) | `README.md`, `docs/onboarding.md`, `docs/decision-log.md`, `docs/traceability-matrix.md`, `executive-summary.html` | Governs all prose deliverables in this build |

## Key parameters

These values and formulas come straight from the prompt. Reviewers can spot-check them against the listed files.

| Parameter | Value | Where |
|-----------|-------|-------|
| Easy tier | depth 4, 3 s | `backend/chess_ai/config.py` |
| Medium tier | depth 6, 8 s | `backend/chess_ai/config.py` |
| Hard tier | depth 8, 15 s | `backend/chess_ai/config.py` |
| `MIN_AI_DELAY_MS` | 1500 | `backend/chess_ai/config.py` |
| `SELF_PLAY_MOVE_DELAY_MS` | 5000 | `backend/chess_ai/config.py` |
| Transposition table cap | 256 MB / 2^20 entries | `backend/chess_ai/engine/search.py` |
| LMR formula | `R = max(1, floor(log(depth) * log(moveIndex) / 2.0))` | `backend/chess_ai/engine/search.py` |
| Null-move reduction | `R = 3 + depth / 6` | `backend/chess_ai/engine/search.py` |
| Futility margins | 200 / 350 / 500 cp at depths 1 / 2 / 3 | `backend/chess_ai/engine/search.py` |
| Aspiration window | ±25 cp | `backend/chess_ai/engine/search.py` |

## Maintenance

This matrix is maintained by hand; update it whenever a constraint's implementation moves to a different file. For the reasoning behind the non-trivial choices, see [`decision-log.md`](decision-log.md).
