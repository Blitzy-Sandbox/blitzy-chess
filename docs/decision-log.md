# Decision Log

This log records the non-trivial architectural choices and explicit deviations behind `blitzy-chess`. Per the Explainability rule, the rationale for a design choice lives here, not in code comments; see [`traceability-matrix.md`](traceability-matrix.md) for the requirement-to-file coverage.

| Decision | Alternatives | Rationale | Risk |
|----------|--------------|-----------|------|
| Backend on `FastAPI` + `Uvicorn` (ASGI) with WebSocket transport for game play. | `Flask`/WSGI serving synchronous HTTP/JSON (the earlier minimal spec). | Real-time multiplayer, streamed AI-thinking updates, and a non-blocking server need ASGI + WebSocket; the prompt supersedes the earlier Flask/WSGI interpretation. **(deviation)** | Async complexity; mitigated by offloading the search via `asyncio.to_thread()`. |
| `python-chess` enforces every rule on the server; the client `chess.js` board is display-only. | A custom hand-written rules engine; trusting client-side moves. | `python-chess` is battle-tested for legality, SAN, FEN, draw detection, and Zobrist hashing, and server authority prevents tampering. **(deviation)** | Third-party dependency; mitigated by pinning `chess>=1.10`. |
| Depend on `chess>=1.10`; code does `import chess`. | Use the `python-chess` distribution name instead. | On PyPI, `python-chess` is a thin stub that only depends on `chess`, so naming it directly is misleading. | Name confusion; mitigated by a note in `docs/onboarding.md`. |
| `backend/chess_ai/engine/` imports only `chess` and the standard library; no `FastAPI`, `Starlette`, or WebSocket code. | Let the engine reach into request or connection objects. | Purity lets the search run in a worker thread and be unit-tested in isolation. | A little extra plumbing to pass data in; acceptable. |
| WebSocket handlers offload the synchronous search with `asyncio.to_thread()`. | Run the search on the event loop; rewrite the search as async. | CPU-bound search would stall the event loop and every connection; threads keep the loop responsive while the search stays synchronous and simple. | The GIL limits CPU parallelism; acceptable for per-game search. |
| Key the transposition table on `board.zobrist_hash()` (the 64-bit value, exposed by `chess.polyglot.zobrist_hash(board)`); cap it at 256 MB / 2^20 entries. | The internal `board._transposition_key()`, which returns a tuple. | A 64-bit integer key is compact and fast; the internal tuple key is unsuitable for a fixed-size table. | Hash collisions (negligible at 64 bits); memory bounded by the cap. |
| Pin `react-chessboard@^4`. | The 5.x line with its single `options`-object API. | 5.x replaced the individual props with one options object; the `frontend/src/components/GameBoard.tsx` wrapper uses the 4.x individual-prop API. | Misses 5.x features; revisit if the wrapper is rewritten. |
| Pin `tailwindcss@^3`. | The 4.x line, which is CSS-first and drops `tailwind.config.js`. | The board palette and theme are configured in `frontend/tailwind.config.js`, which the 4.x CSS-first model removes. | Misses 4.x; revisit if migrating the theme to CSS-first. |
| Pin `react` / `react-dom@^18` (and `@types/react` / `@types/react-dom@^18`). | React 19, the current line. | The prompt's floor is React 18; pinning keeps the test and type toolchain on a known-good line. | Misses React 19; low, since 18 is fully supported. |
| Hold game and room state in memory; only self-play recordings persist, as files in `backend/games/`. | A database or ORM for durable games and users. | The prompt specifies in-process state, and no persistence layer is in scope. | State is lost on restart; acceptable for anonymous play. |
| Ship observability now: structured logging with correlation IDs, tracing, a `/metrics` endpoint, health and readiness checks, and a dashboard template. | Defer observability to a later revision (the earlier spec). | The Observability project rule mandates it in the first release. **(deviation)** | Extra dependencies, and `/metrics` is unauthenticated (see the next row). |
| Expose `/metrics` without authentication. | Protect it behind auth or secrets. | Scope is local verification; production hardening is out of scope. | Would expose metrics if deployed as-is; recorded as out-of-scope future work. |
| Keep `backend/chess_ai/rooms/protocol.py` and `frontend/src/types/index.ts` in step by hand. | Generate the types from a shared schema. | The message set is small and stable, so code generation is overkill for this scope. | Drift if edited on one side only; mitigated by documenting the pairing. |
| Create `blitzy-deck/references/blitzy-reveal-theme.css` from the theme spec embedded in the Executive Presentation rule. | Omit the file or link an external theme. | The file is referenced but absent from the repo, and the rule embeds the full spec, so the build creates it. | Must match the embedded spec; low. |
| Route the five screens with a `useState` view machine in `frontend/src/App.tsx`; add no router library. | A router such as `react-router`. | Five flat screens need no nested routes or URL history, so a view-state switch is simpler. | No deep-linkable URLs; acceptable for this scope. |
| Build and serve the frontend with `Vite`; its dev server proxies `/ws/` and `/api/` to `localhost:8000`. | Create React App or a hand-rolled Webpack setup. | `Vite` gives fast dev startup and a simple proxy that gives the browser one origin for both WebSocket and REST. | None significant; `Vite` is the modern default. |

## Deviations from the earlier specification

The build deliberately departs from the earlier minimal interpretation. Each deviation is intentional:

- `FastAPI` + WebSocket replaces the earlier `Flask`/WSGI HTTP-JSON server, because real-time play and streamed updates need ASGI.
- `python-chess` replaces the hand-written rules engine as the authoritative source of legality, SAN, FEN, draw detection, and Zobrist hashing.
- A hand-built AI engine (evaluation, search, opening book, optional Syzygy endgames) is added, where the earlier spec had none.
- Observability ships in the first release instead of being deferred.

The prompt and the project rules are authoritative, and the repository is greenfield, so these are choices, not conflicts with existing code.

## Maintenance

Update this log whenever a non-trivial decision changes, and keep the rationale here rather than in code comments.
