# noeta-agent developer convenience wrapper.
#
# The underlying entry point stays the single, zero-argument, env-only
# `python -m noeta.agent`. This Makefile adds no CLI flags to that entry point;
# it only does two things: build the frontend in one step, and translate
# convenience options into the existing NOETA_AGENT_* environment variables.
#
# Common usage:
#   make run                         # one step: build frontend + start backend (reads ./noeta.config.json by default)
#   make run CONFIG=path/to.json     # specify a config
#   make run PORT=9000               # override the port for this run
#   make serve                       # backend only (skip the build when the frontend is already current)
#   make dev                         # hot reload: backend(8765) + vite dev(5173 auto proxy)
#   make install                     # first time: install editable package + frontend deps

PY      ?= uv run python
WEB_DIR := apps/web

# Reads ./noeta.config.json at the repo root by default (only passed to the backend if it exists).
CONFIG  ?= noeta.config.json
PORT    ?= 8765
HOST    ?= 127.0.0.1
HAS_CONFIG := $(wildcard $(CONFIG))

# --- env translation ------------------------------------------------------
# Priority must match the backend: explicit command-line args > config file > fallback defaults.
# The backend prioritizes env over the config file, so we only export the port/host when the
# value is passed explicitly on the command line OR there is no config file, to avoid blindly
# overriding the values in the config.

ifneq ($(HAS_CONFIG),)
export NOETA_AGENT_CONFIG := $(CONFIG)
endif

ifeq ($(origin PORT),command line)
export NOETA_AGENT_PORT := $(PORT)
else ifeq ($(HAS_CONFIG),)
export NOETA_AGENT_PORT := $(PORT)
endif

ifeq ($(origin HOST),command line)
export NOETA_AGENT_HOST := $(HOST)
else ifeq ($(HAS_CONFIG),)
export NOETA_AGENT_HOST := $(HOST)
endif

# These only override when passed explicitly on the command line (otherwise respect config / backend defaults).
ifeq ($(origin WORKSPACE),command line)
export NOETA_AGENT_WORKSPACE := $(WORKSPACE)
endif
ifeq ($(origin PROVIDER),command line)
export NOETA_AGENT_PROVIDER := $(PROVIDER)
endif
ifeq ($(origin MODEL),command line)
export NOETA_AGENT_MODEL := $(MODEL)
endif

.DEFAULT_GOAL := help
.PHONY: help run serve web dev install check

help:
	@echo "noeta-agent — one-step build + start (still python -m noeta.agent underneath, pure env boundary)"
	@echo ""
	@echo "  make run        build frontend + start backend (reads ./noeta.config.json by default, port 8765)"
	@echo "  make serve      backend only (do not rebuild the frontend)"
	@echo "  make web        build the frontend to dist/ only"
	@echo "  make dev        hot reload: backend(8765) + vite dev(5173 auto proxy)"
	@echo "  make install    first time: uv pip install -e apps/noeta-agent + frontend deps"
	@echo "  make check      the local CI gate: pytest+coverage, mypy(protocols), naming/import lints"
	@echo ""
	@echo "  overridable variables: CONFIG= PORT= HOST= WORKSPACE= PROVIDER= MODEL="
	@echo "  e.g.: make run CONFIG=examples/openai-compatible/config.json"

## one step: build frontend + start backend
run: web serve

## start the backend only (do not rebuild the frontend)
serve:
	@echo "▶ noeta.agent → http://$(HOST):$(PORT)/chat  (config: $(if $(HAS_CONFIG),$(CONFIG),<none — stub offline default>))"
	$(PY) -m noeta.agent

## build the frontend to dist/ (installs deps automatically on first run)
web:
	@[ -d $(WEB_DIR)/node_modules ] || (cd $(WEB_DIR) && npm install)
	cd $(WEB_DIR) && npm run build

## hot-reload development: backend(8765) in the background + vite dev in the foreground, Ctrl-C stops both.
## The backend is forced to 8765 to match vite's hardcoded proxy; provider/model/key in the config still apply.
dev:
	@[ -d $(WEB_DIR)/node_modules ] || (cd $(WEB_DIR) && npm install)
	@echo "▶ backend :8765 + vite dev (proxy → 8765). Ctrl-C stops both."
	@NOETA_AGENT_PORT=8765 $(PY) -m noeta.agent & \
	  AGENT_PID=$$!; \
	  trap 'kill $$AGENT_PID 2>/dev/null' EXIT INT TERM; \
	  cd $(WEB_DIR) && npm run dev

## first-time install: editable package + frontend deps
install:
	uv pip install -e apps/noeta-agent
	cd $(WEB_DIR) && npm ci

## the local CI gate — mirrors .github/workflows/ci.yml minus what needs CI infrastructure.
## CI-only steps (expected to be absent locally): the Postgres storage contract tests
## (skipped unless NOETA_TEST_POSTGRES_DSN points at a live server), the Playwright
## web e2e smoke, and the fresh-venv install smoke.
check:
	uv run pytest --cov=noeta --cov-report=term --cov-fail-under=85
	MYPYPATH=packages/noeta-runtime uv run mypy --strict \
	  --namespace-packages --explicit-package-bases \
	  packages/noeta-runtime/noeta/protocols
	uv run python scripts/lint-naming.py
	uv run lint-imports --config .importlinter
