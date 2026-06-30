# ============================================================================
#  Blitzy Chess — the single control surface for the monorepo.
#
#  Every operation runs through this Makefile (project Constraint 9): the
#  README and the onboarding guide reference make targets only, never raw
#  commands. This file orchestrates the Python backend (FastAPI, served from a
#  virtualenv at backend/.venv) and the React + TypeScript frontend (Vite/npm).
#
#  Shortest path from a clean checkout (run at the repository root):
#      make init     # one-time: venv, all deps, Playwright browser, opening book
#      make dev      # backend (:8000) + Vite dev server together
#
#  Run `make help` for the full, self-documenting list of targets.
# ============================================================================

# Bash is required for the trap-based `dev` recipe and the venv bootstrap
# fallback used on systems where the platform Python's ensurepip is unavailable.
SHELL := /bin/bash

# --- Directories & ports ----------------------------------------------------
BACKEND_DIR  := backend
FRONTEND_DIR := frontend
# Absolute path to the backend package root. Exported as PYTHONPATH for the
# pytest run so `import chess_ai` resolves: pytest is launched through its
# console-script entry point which — unlike `python -m` and unlike Uvicorn's
# app loader — does NOT put the working directory on sys.path.
BACKEND_ABS  := $(abspath $(BACKEND_DIR))
PORT         ?= 8000

# Interpreter used ONLY to create the virtualenv. Override for a specific
# version, e.g. `make init PYTHON=python3.13`.
PYTHON       ?= python3

# --- Backend virtualenv (backend/.venv) -------------------------------------
# VENV is repo-relative: used by recipes that run from the repository root and
# by `clean`. VENV_BIN is ABSOLUTE (via $(abspath ...)) so the tool paths below
# stay valid inside recipes that `cd` into a subdirectory before invoking them
# (e.g. `start`, `self-play`, `test-backend`, `lint`, `format`).
VENV         := $(BACKEND_DIR)/.venv
VENV_BIN     := $(BACKEND_ABS)/.venv/bin
PY           := $(VENV_BIN)/python
PIP          := $(VENV_BIN)/pip
UVICORN      := $(VENV_BIN)/uvicorn
RUFF         := $(VENV_BIN)/ruff
PYTEST       := $(VENV_BIN)/pytest
PLAYWRIGHT   := $(VENV_BIN)/playwright

# Fallback source for pip when `python -m venv` cannot provision it itself.
GET_PIP_URL  := https://bootstrap.pypa.io/get-pip.py

# --- Canned recipe: provision the backend virtualenv -----------------------
# An IMPLEMENTATION VARIABLE (not a make target) so the backend venv bootstrap
# is shared by the public `init` and `download-syzygy` targets without adding a
# `venv` target to the public surface. Idempotent and resilient to a broken
# platform `ensurepip`: it creates the venv (falling back to `--without-pip`),
# then bootstraps pip via `ensurepip` or, failing that, get-pip.py. Each line is
# a single logical shell statement so the canned recipe expands cleanly.
define ensure_venv
@test -x $(PY) || { echo "==> Creating virtualenv at $(VENV)"; $(PYTHON) -m venv $(VENV) || $(PYTHON) -m venv --without-pip $(VENV); }
@test -x $(PIP) || $(PY) -m ensurepip --upgrade >/dev/null 2>&1 || { echo "==> ensurepip unavailable; bootstrapping pip via get-pip.py"; curl -fsSL $(GET_PIP_URL) -o /tmp/get-pip.py && $(PY) /tmp/get-pip.py; }
@$(PY) -m pip install --upgrade pip setuptools wheel
endef

# --- Canned recipe: ensure a system ffmpeg with an MP4 muxer is installed ----
# The self-play recorder (`make self-play`) records the browser as WebM via
# Playwright and then transcodes to the MP4 the project mandates (AAP §0.1.2
# Constraint 14). Playwright's bundled ffmpeg muxes only WebM, so a full system
# ffmpeg (with an MP4 muxer + libx264) is required to produce the artifact. This
# installs one when missing. It is best-effort and OS-aware: an already-present
# ffmpeg short-circuits; apt-get (Debian/Ubuntu) and Homebrew (macOS) are
# handled; anything else prints actionable guidance WITHOUT failing `init`,
# since ffmpeg is only needed for `self-play` (not dev/build/test).
define ensure_ffmpeg
@if command -v ffmpeg >/dev/null 2>&1; then \
	echo "==> ffmpeg already present: $$(command -v ffmpeg)"; \
elif command -v apt-get >/dev/null 2>&1; then \
	echo "==> Installing system ffmpeg via apt-get (required by 'make self-play' to mux MP4)"; \
	if [ "$$(id -u)" = "0" ]; then apt-get update && apt-get install -y ffmpeg; \
	elif command -v sudo >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y ffmpeg; \
	else echo "WARNING: root privileges are required to apt-get install ffmpeg; install it manually so 'make self-play' can mux MP4."; fi; \
elif command -v brew >/dev/null 2>&1; then \
	echo "==> Installing system ffmpeg via Homebrew (required by 'make self-play' to mux MP4)"; \
	brew install ffmpeg; \
else \
	echo "WARNING: no apt-get or brew found; install a system ffmpeg with an MP4 muxer manually so 'make self-play' can produce the required MP4."; \
fi
endef

.DEFAULT_GOAL := help

.PHONY: help init dev build start self-play test test-backend test-frontend \
	lint format download-syzygy clean all

# ============================================================================
#  Help
# ============================================================================

help: ## Show this help — every target with a one-line description
	@echo "Blitzy Chess — available make targets:"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Prerequisites: run 'make init' once before dev / start / test / self-play."

# ============================================================================
#  Setup
# ============================================================================

init: ## One-time setup from a clean machine: venv, backend+dev deps, Playwright, system ffmpeg, frontend deps, opening book
	$(ensure_venv)
	$(PIP) install -r $(BACKEND_DIR)/requirements.txt -r $(BACKEND_DIR)/requirements-dev.txt
	$(PLAYWRIGHT) install chromium
	$(ensure_ffmpeg)
	cd $(FRONTEND_DIR) && npm install
	$(PY) $(BACKEND_DIR)/scripts/download_book.py
	@echo "==> Setup complete. Next: 'make dev' (development) or 'make start' (production-style)."

# ============================================================================
#  Development & run
# ============================================================================

dev: ## Run the backend (:8000) and the Vite dev server together; Ctrl-C stops both
	@echo "==> Backend:  http://localhost:$(PORT)  (Uvicorn, auto-reload)"
	@echo "==> Frontend: Vite dev server (usually http://localhost:5173)"
	@echo "==> Vite proxies /ws/ and /api/ to http://localhost:$(PORT) — see frontend/vite.config.ts"
	@trap 'kill 0' EXIT INT TERM; \
		( cd $(BACKEND_DIR) && exec $(UVICORN) chess_ai.app:app --reload --port $(PORT) ) & \
		( cd $(FRONTEND_DIR) && exec npm run dev ) & \
		wait

build: ## Build the production frontend bundle into frontend/dist
	cd $(FRONTEND_DIR) && npm run build

start: build ## Build, then serve API + WebSocket + static SPA via one Uvicorn process (no reload)
	cd $(BACKEND_DIR) && $(UVICORN) chess_ai.app:app --port $(PORT)

self-play: build ## Build the frontend, then run the AI self-play demo (drives the browser; records backend/games/self_play_*.mp4 + transcript)
	cd $(BACKEND_DIR) && $(PY) -m chess_ai.self_play.runner

# ============================================================================
#  Quality — tests, lint, format
# ============================================================================

test: test-backend test-frontend ## Run the full backend and frontend test suites

test-backend: ## Run the backend test suite (pytest; config in backend/pyproject.toml)
	cd $(BACKEND_DIR) && PYTHONPATH=$(BACKEND_ABS) $(PYTEST)

test-frontend: ## Run the frontend test suite (Vitest, single run)
	cd $(FRONTEND_DIR) && npm run test

lint: ## Lint the backend (ruff) and the frontend (eslint)
	cd $(BACKEND_DIR) && $(RUFF) check .
	cd $(FRONTEND_DIR) && npm run lint

format: ## Format the backend (ruff) and the frontend (prettier)
	cd $(BACKEND_DIR) && $(RUFF) format .
	cd $(FRONTEND_DIR) && npm run format

# ============================================================================
#  Data assets
# ============================================================================

download-syzygy: ## Download the Syzygy endgame tablebases into backend/tables/
	$(ensure_venv)
	$(PY) $(BACKEND_DIR)/scripts/download_syzygy.py

# ============================================================================
#  Housekeeping
# ============================================================================

clean: ## Remove venv, node_modules, build output, caches, and self-play recordings (keeps book + tables)
	rm -rf $(VENV)
	rm -rf $(FRONTEND_DIR)/node_modules $(FRONTEND_DIR)/dist
	find $(BACKEND_DIR) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find $(BACKEND_DIR) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND_DIR)/.ruff_cache .ruff_cache
	rm -rf $(BACKEND_DIR)/.coverage $(BACKEND_DIR)/.coverage.* $(BACKEND_DIR)/coverage.xml $(BACKEND_DIR)/htmlcov
	rm -f $(BACKEND_DIR)/games/*.mp4 $(BACKEND_DIR)/games/*.md
	@echo "==> Cleaned build artifacts, caches, and recordings. Opening book and Syzygy tables were preserved."

# ============================================================================
#  Aggregate
# ============================================================================

all: ## Set up, lint, test, and build everything (init -> lint -> test -> build)
	@$(MAKE) --no-print-directory init
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory build
	@echo "==> all: init, lint, test, and build complete."
