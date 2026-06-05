# Developer Onboarding

`blitzy-chess` is a web chess application. The backend is Python and [FastAPI](https://fastapi.tiangolo.com/): it runs a hand-built chess AI and holds the authoritative game state, and it talks to the browser over WebSocket. The frontend is a React and TypeScript single-page app built with [Vite](https://vitejs.dev/) that renders the board with `react-chessboard`. You can play the AI in three difficulty tiers, play another person in real time, or watch the AI play itself in a demo that records video and writes a timestamped commentary transcript.

This guide takes you from a clean machine to a running, modifiable app. The [`README.md`](../README.md) has the short quick start. This file goes deeper: setup, the project's domain, the common pitfalls, how to extend the code, and where to go next. For the reasoning behind the architecture, read the [decision log](./decision-log.md); for how each requirement maps to code, read the [traceability matrix](./traceability-matrix.md).

## Prerequisites

Install these four tools on your machine first. The project assumes nothing else.

| Tool | Version | Why |
|------|---------|-----|
| Python | 3.11 or newer | Runs the backend and the chess engine. The code uses 3.11+ features such as `enum.StrEnum`. |
| Node.js | 18 or newer | Runs the frontend build and dev server. Ships with `npm`. |
| GNU Make | any recent | The single control surface. Every operation is a `make` target. |
| git | any recent | Clones the repository. |

You install nothing else by hand. `make init` creates an isolated backend virtual environment at `backend/.venv` and installs every backend and frontend dependency locally. You never manage the virtual environment or call a package manager yourself — `make` does it for you.

The self-play demo drives a real browser with Playwright and captures the screen. `make init` installs Playwright and provisions the browser it needs, and `make self-play` handles the full-UI rendering and recording. You do not set any of that up by hand.

## First run: clean machine to running app

Run everything from the repository root. After you clone the repo, the first project command is always `make init`.

```sh
# One-time bootstrap (the only raw command you type):
git clone <repo-url> && cd blitzy-chess

# One-time setup: virtual environment, all dependencies, opening book.
make init

# Develop with hot reload (backend + frontend together).
make dev
```

`make dev` starts the FastAPI backend on port `8000` and the Vite dev server for the SPA. Vite prints a local URL — usually `http://localhost:5173` — so open that one in your browser. The Vite dev server proxies `/ws/` and `/api/` to `localhost:8000`, so the browser sees a single origin for both the WebSocket and the REST calls.

To run the production-style path instead, build the bundle and then serve it:

```sh
# Build the frontend bundle into frontend/dist.
make build

# Serve the built frontend, the API, and the WebSocket routes together on :8000.
make start
```

`make start` serves the built SPA as static files with a single-page-application fallback, alongside the API and WebSocket routes, all on `http://localhost:8000`. Open that URL in your browser.

To watch the engine play itself:

```sh
# Record an AI self-play game to video plus a commentary transcript.
make self-play
```

`make self-play` writes an MP4 and a timestamped commentary transcript into `backend/games/`.

The shortest path to a running app is two commands: `make init`, then `make dev`.

## All make targets

Every operation runs through `make`. You never type a raw build, test, or run command.

| Target | What it does |
|--------|--------------|
| `make init` | One-time setup: create `backend/.venv`, install backend runtime and dev dependencies, install frontend npm packages, and download the Polyglot opening book. |
| `make dev` | Run the backend and frontend dev servers together. Vite proxies `/ws/` and `/api/` to `localhost:8000`. |
| `make build` | Build the production frontend bundle into `frontend/dist`. |
| `make start` | Serve the built frontend (static files with SPA fallback) with the API and WebSocket routes on port 8000. |
| `make self-play` | Run the AI self-play demo; record an MP4 and a transcript into `backend/games/`. |
| `make test` | Run the full backend and frontend test suites. |
| `make test-backend` | Run the backend test suite only. |
| `make test-frontend` | Run the frontend test suite only. |
| `make lint` | Lint the backend and frontend code. |
| `make format` | Format the backend and frontend code. |
| `make download-syzygy` | Download the optional Syzygy endgame tablebases into `backend/tables/`. |
| `make clean` | Remove the virtual environment, node modules, build output, caches, and game recordings. |
| `make all` | Convenience aggregate: set up, build, and test everything. |

Under the hood, `make lint` and `make format` wrap the real tools — ruff for Python, eslint and prettier for TypeScript — but you invoke only the make target. The backend tests run under pytest and the frontend tests run under Vitest, again behind `make test`. One extra target exists for the opening book: `make download-book` fetches it on its own, and `make init` already runs it for you, so you rarely call it directly.

## Difficulty tiers

Each tier caps the AI by how deep it searches and how long it may think.

| Tier | Search depth | Time budget |
|------|--------------|-------------|
| Easy | 4 | 3 seconds |
| Medium | 6 | 8 seconds |
| Hard | 8 | 15 seconds |

These tiers are defined in `backend/chess_ai/config.py` and chosen from the Mode Select screen. The self-play demo plays Hard against Medium.

## Domain context

A little background makes the codebase easy to read.

**The server is authoritative.** python-chess on the backend is the single source of truth. It decides legality, generates SAN, builds FEN strings, detects draws, and computes Zobrist hashes. The frontend keeps a `chess.js` mirror of the position for display and SAN only — it never decides the game. Both WebSocket endpoints validate every inbound move with `board.is_legal()` before they apply or relay it, and they reject anything illegal.

**Moves travel over WebSocket.** The browser sends and receives game moves only over WebSocket: `/ws/game` for games against the AI and `/ws/multiplayer` for room play. REST is reserved for a few non-game calls — health, readiness, the initial load, and `/metrics`. Never add a REST endpoint that makes a move.

**The AI engine is pure computation.** It lives in `backend/chess_ai/engine/` and imports no web framework, so it can run in a worker thread and be tested on its own. It has four parts:

- An **opening book** (a Polyglot `.bin` file), probed before the search on every move.
- A tuned **evaluation** function: material, piece-square tables interpolated by game phase, pawn structure cached on a pawn-only key, king safety, and mobility.
- A modern **alpha-beta search**: negamax with iterative deepening, aspiration windows, principal variation search, quiescence, a transposition table keyed on the Zobrist hash, null-move pruning, late move reduction, killer and history move ordering, and search extensions.
- Optional **Syzygy endgame tablebases**, consulted when few pieces remain.

**The search never blocks the event loop.** The WebSocket handlers run the synchronous search through `asyncio.to_thread()`, so the FastAPI event loop stays responsive while the engine thinks.

**Where things live.** This map points you to the right file fast.

| Path | Holds |
|------|-------|
| `backend/chess_ai/app.py` | The composition root: app setup, CORS, lifespan resource loading, router registration, static serving. |
| `backend/chess_ai/config.py` | Ports, timing constants, difficulty tiers, and file paths. |
| `backend/chess_ai/api/` | The WebSocket and REST endpoints. |
| `backend/chess_ai/engine/` | The search and evaluation (pure computation). |
| `backend/chess_ai/rooms/` | The multiplayer room manager and the message protocol. |
| `backend/chess_ai/self_play/` | The self-play runner and the transcript annotator. |
| `backend/chess_ai/observability/` | Logging, tracing, and metrics. |
| `frontend/src/components/` | The React UI. |
| `frontend/src/hooks/` | The WebSocket transport and the local display mirror. |
| `frontend/src/types/index.ts` | The message contract, mirroring `backend/chess_ai/rooms/protocol.py`. |

## Common pitfalls

- **Run `make init` before `make dev` or `make start`.** Without the virtual environment and the frontend packages, the servers have nothing to run.
- **The opening book and Syzygy tables are optional downloads.** `make init` fetches the book; `make download-syzygy` fetches the tables. The engine degrades gracefully when they are absent, so do not assume they are present.
- **python-chess installs under the name `chess`.** The requirements pin `chess>=1.10`, and the code does `import chess`. Do not reach for the `python-chess` distribution name — on PyPI that name is a thin stub. The make flow installs the right package; in code, just `import chess`.
- **Keep the engine pure.** Nothing in `backend/chess_ai/engine/` may import FastAPI, Starlette, or WebSocket code. Purity is what lets the search run in a worker thread and be unit-tested in isolation.
- **Game moves go over WebSocket only.** REST handles health, readiness, the initial load, and metrics — never a move.
- **Two dependencies are pinned on purpose.** `react-chessboard` stays on the 4.x line and Tailwind CSS stays on the 3.x line, because their newer majors change the board API and the theme model this project relies on. The [decision log](./decision-log.md) records why. Do not upgrade them without reading it.
- **In `make dev`, two servers run.** The SPA runs on Vite at `:5173` and the backend at `:8000`, and the Vite proxy forwards `/ws/` and `/api/`. If WebSocket calls fail in development, check the proxy config in `frontend/vite.config.ts`.

## How to extend

- **Tune the AI.** Adjust the evaluation weights and the piece-square tables in `backend/chess_ai/engine/tables.py` and `backend/chess_ai/engine/evaluator.py`. Tweak the search parameters in `backend/chess_ai/engine/search.py`. Then run `make test-backend` — the tactical suite in `backend/tests/test_search.py` guards correctness.
- **Add a UI feature.** Add or change components in `frontend/src/components/`, and put transport changes in `frontend/src/hooks/`. Render the board only through `react-chessboard`, never a hand-rolled canvas or SVG. Then run `make test-frontend`.
- **Change the message protocol.** Edit `backend/chess_ai/rooms/protocol.py` and the mirror in `frontend/src/types/index.ts` together. The two sides are kept in step by hand, so an edit on one side needs the matching edit on the other.
- **Keep the rules.** Preserve the server-authoritative model and the engine's purity in every change. Validate with `make lint`, `make format`, and `make test` before you commit. Record any non-trivial decision in the [decision log](./decision-log.md); the rationale belongs there, not in code comments.

## Suggested next tasks

Good starting points for further work:

- Expand the opening book, or add a setting to choose between books.
- Add more tactical positions to `backend/tests/test_search.py`.
- Tune the evaluation weights and measure the effect with self-play.
- Add an optional on-screen chess clock. Note that player-facing clocks are out of scope today, so this is new ground.

A few larger ideas sit outside the current scope but would be natural later additions: persisting games, user accounts, matchmaking and ratings, and cloud deployment. Treat them as future work, not part of this build.

## Where to go next

- [`../README.md`](../README.md) — the project overview and quick start.
- [`./decision-log.md`](./decision-log.md) — the architecture decisions and the reasoning behind them.
- [`./traceability-matrix.md`](./traceability-matrix.md) — how each requirement maps to its implementation.
- [`../executive-summary.html`](../executive-summary.html) — the leadership overview deck.
