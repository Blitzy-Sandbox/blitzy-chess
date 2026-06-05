# ============================================================================
# Blitzy Chess - single control surface.
# Every operation runs through this Makefile (project constraint #9).
#
# Common targets:
#   make init             Create the backend venv, install all deps, fetch book
#   make dev              Run backend + frontend dev servers together
#   make build            Build the frontend for production
#   make start            Serve the built frontend + API/WebSocket (prod-style)
#   make self-play        Run the AI self-play demonstration + recording
#   make test             Run backend and frontend test suites
#   make lint             Lint backend (ruff) and frontend (eslint)
#   make format           Format backend (ruff) and frontend (prettier)
#   make download-syzygy  Fetch Syzygy endgame tablebases
#   make clean            Remove venv, node_modules, build output, caches
#   make all              init + build + test
# ============================================================================

PYTHON ?= python3
PORT ?= 8000

BACKEND_DIR := backend
FRONTEND_DIR := frontend
VENV := $(BACKEND_DIR)/.venv
VENV_PY := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip
GET_PIP_URL := https://bootstrap.pypa.io/get-pip.py

.DEFAULT_GOAL := help

.PHONY: help init init-backend init-frontend venv dev dev-backend dev-frontend \
	    build start self-play test test-backend test-frontend lint lint-backend \
	    lint-frontend format format-backend format-frontend download-book \
	    download-syzygy clean all

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	    awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup ------------------------------------------------------------------

init: init-backend init-frontend download-book ## Full setup from a clean machine

venv: ## Create the backend virtual environment (with pip bootstrap fallback)
	@test -x $(VENV_PY) || ( \
	    $(PYTHON) -m venv $(VENV) || $(PYTHON) -m venv --without-pip $(VENV) ; \
	    $(VENV_PY) -m ensurepip --upgrade 2>/dev/null || \
	        ( curl -sS $(GET_PIP_URL) -o /tmp/get-pip.py && $(VENV_PY) /tmp/get-pip.py ) ; \
	)
	$(VENV_PY) -m pip install --upgrade pip setuptools wheel

init-backend: venv ## Install backend runtime + dev dependencies and Playwright
	$(VENV_PIP) install -r $(BACKEND_DIR)/requirements-dev.txt
	$(VENV)/bin/playwright install chromium

init-frontend: ## Install frontend dependencies
	cd $(FRONTEND_DIR) && npm install

# --- Development ------------------------------------------------------------

dev: ## Run backend and frontend dev servers together
	@echo "Starting backend (:$(PORT)) and frontend dev servers..."
	@$(MAKE) -j2 dev-backend dev-frontend

dev-backend: ## Run the FastAPI backend with autoreload
	cd $(BACKEND_DIR) && .venv/bin/uvicorn chess_ai.app:app --reload --port $(PORT)

dev-frontend: ## Run the Vite dev server
	cd $(FRONTEND_DIR) && npm run dev

# --- Build & run ------------------------------------------------------------

build: ## Build the frontend for production
	cd $(FRONTEND_DIR) && npm run build

start: ## Serve the built frontend + API/WebSocket (production-style)
	cd $(BACKEND_DIR) && .venv/bin/uvicorn chess_ai.app:app --port $(PORT)

self-play: ## Run the AI self-play demonstration with screen recording
	cd $(BACKEND_DIR) && .venv/bin/python -m chess_ai.self_play.runner

# --- Tests ------------------------------------------------------------------

test: test-backend test-frontend ## Run all test suites

test-backend: ## Run backend tests (pytest)
	cd $(BACKEND_DIR) && .venv/bin/python -m pytest

test-frontend: ## Run frontend tests (vitest)
	cd $(FRONTEND_DIR) && npm run test

# --- Lint & format ----------------------------------------------------------

lint: lint-backend lint-frontend ## Lint backend and frontend

lint-backend: ## Lint backend with ruff
	cd $(BACKEND_DIR) && .venv/bin/ruff check .

lint-frontend: ## Lint frontend with eslint
	cd $(FRONTEND_DIR) && npm run lint

format: format-backend format-frontend ## Format backend and frontend

format-backend: ## Format backend with ruff
	cd $(BACKEND_DIR) && .venv/bin/ruff format .

format-frontend: ## Format frontend with prettier
	cd $(FRONTEND_DIR) && npm run format

# --- Data assets ------------------------------------------------------------

download-book: ## Fetch the Polyglot opening book
	$(VENV_PY) $(BACKEND_DIR)/scripts/download_book.py

download-syzygy: ## Fetch Syzygy endgame tablebases
	$(VENV_PY) $(BACKEND_DIR)/scripts/download_syzygy.py

# --- Housekeeping -----------------------------------------------------------

clean: ## Remove venv, node_modules, build output, and caches
	rm -rf $(VENV)
	rm -rf $(FRONTEND_DIR)/node_modules $(FRONTEND_DIR)/dist
	find $(BACKEND_DIR) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find $(BACKEND_DIR) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND_DIR)/.ruff_cache $(BACKEND_DIR)/.coverage

all: init build test ## Setup, build, and test everything
