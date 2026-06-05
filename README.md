# blitzy-chess

Blitzy takes on chess. Play a hand-built AI, challenge a friend in real time, or watch the engine play itself.

## What it is

blitzy-chess is a web chess application with three modes:

- **Play the AI.** A chess engine built from scratch, offered in three difficulty tiers.
- **Play a human.** Real-time multiplayer between two people over a shared connection.
- **Watch the AI play itself.** A self-play demonstration that records the screen to a video file and writes a timestamped commentary transcript.

## Architecture at a glance

- **Backend** (`backend/`): a Python **FastAPI** application served by **Uvicorn**. It owns the chess AI and the authoritative game state, and it speaks to the browser over WebSocket. The `chess_ai/engine/` package is pure computation — it imports no web framework, so it can run in a worker thread and be tested on its own.
- **Frontend** (`frontend/`): a **React + TypeScript** single-page app built with **Vite**. It renders the board with `react-chessboard`.
- **The server is authoritative.** The backend validates every move and holds the true position. The browser keeps a `chess.js` mirror for display only.

## Difficulty tiers

Each tier caps the AI by how deep it searches and how long it may think.

| Tier | Search Depth | Time Budget |
|------|--------------|-------------|
| Easy | 4 | 3 seconds |
| Medium | 6 | 8 seconds |
| Hard | 8 | 15 seconds |

## Prerequisites

You need these on a clean machine:

- **Python 3** (3.11 or newer recommended)
- **Node.js** (18 or newer)
- **make**
- **git**

That is all you install by hand. `make init` pulls in every backend and frontend dependency for you. Do not install project packages yourself.

## Quick start

Run these from the repository root.

One-time setup:

1. `make init` — creates the Python virtual environment, installs the backend and frontend dependencies, and downloads the opening book.

Develop with hot reload:

2. `make dev` — runs the backend and frontend together. The Vite dev server proxies `/ws/` and `/api/` to `localhost:8000` and prints a local URL (usually `http://localhost:5173`). Open that URL in your browser.

Or run the production-style build:

3. `make build` — builds the frontend bundle.
4. `make start` — serves the built frontend, the API, and the WebSocket routes together at `http://localhost:8000`. Open that URL in your browser.

Watch the engine play itself:

5. `make self-play` — runs the self-play demonstration and records an MP4 plus a commentary transcript into `backend/games/`.

## Make targets

Every operation runs through `make`. This README never asks you to type a raw command.

| Target | What it does |
|--------|--------------|
| `make init` | One-time setup from a clean machine: virtual environment, backend and frontend dependencies, and the opening book. |
| `make dev` | Run the backend and frontend dev servers together. |
| `make build` | Build the frontend for production. |
| `make start` | Serve the built frontend with the API and WebSocket routes (production-style). |
| `make self-play` | Run the AI self-play demonstration and record it into `backend/games/`. |
| `make test` | Run the backend and frontend test suites. |
| `make test-backend` | Run the backend test suite. |
| `make test-frontend` | Run the frontend test suite. |
| `make lint` | Lint the backend and frontend. |
| `make format` | Format the backend and frontend. |
| `make download-syzygy` | Fetch the optional Syzygy endgame tablebases. |
| `make clean` | Remove the virtual environment, node modules, build output, and caches. |
| `make all` | Set up, build, and test everything. |

## Domain context

A little chess-engine background helps when you read the code.

- **python-chess is the single source of truth.** It handles move legality, SAN notation, FEN strings, draw detection, and Zobrist hashing on the server.
- **The AI has four parts.** An opening book plays known early moves. A tuned evaluation function scores a position. An alpha-beta search looks ahead. Optional Syzygy tablebases give perfect play in simple endgames.
- **The frontend never judges a move.** It mirrors the position with `chess.js` for display and SAN only. The server decides what is legal.

## Common pitfalls

- **Run `make init` before `make dev`.** Without setup the servers have nothing to run.
- **The opening book and Syzygy tables are optional downloads.** `make init` fetches the book; `make download-syzygy` fetches the tablebases. The engine still works without them.
- **Keep the engine pure.** The `chess_ai/engine/` package must not import FastAPI, Starlette, or WebSocket code. It stays a plain computation library.
- **Game moves travel over WebSocket only.** REST is limited to health checks and the initial load.
- **python-chess installs under the name `chess`.** In code you write `import chess`, not `import python_chess`.
- **Two dependencies are pinned on purpose.** `react-chessboard` stays on the 4.x line and Tailwind CSS stays on the 3.x line, because their newer majors change the APIs this project relies on.

## How to extend

Here is where things live:

- `backend/chess_ai/engine/` — search and evaluation.
- `backend/chess_ai/api/` — the WebSocket and REST endpoints.
- `frontend/src/components/` — the React UI.
- `frontend/src/hooks/` — the WebSocket transport and the local game state.

When you add a feature, keep two rules. The server stays authoritative: validate every move with python-chess on the backend. The engine stays pure: add no web imports to `chess_ai/engine/`. Use `make test`, `make lint`, and `make format` as your validation loop before you commit.

## Suggested next tasks

Good starting points for further work:

- Tune the evaluation weights and the piece-square tables.
- Add more tactical test positions to the search suite.
- Extend the opening book with more lines.
- Add a player-facing chess clock and time controls.

A few ideas sit outside the current scope but would be natural later additions: user accounts, a database, matchmaking, ratings, and cloud deployment. Treat them as future work, not part of this build.

## Further documentation

- [docs/onboarding.md](docs/onboarding.md) — the full guide from a clean machine to a running, modifiable app.
- [docs/decision-log.md](docs/decision-log.md) — the architecture decisions and the reasoning behind them.
- [docs/traceability-matrix.md](docs/traceability-matrix.md) — how each requirement maps to its implementation.
- [executive-summary.html](executive-summary.html) — the leadership presentation deck.
