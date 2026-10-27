# noeta library repo — developer convenience wrapper.
#
# This repo ships two libraries: packages/noeta-runtime and packages/noeta-sdk.
# The shared dev toolchain lives at the workspace virtual root (pyproject.toml),
# and the test suite lives at the repo root in tests/.
#
# Common usage:
#   make install    first time: uv sync (kernel + dev group)
#   make check      the local CI gate (mirrors .github/workflows/ci.yml)
#   make test       run the test suite with coverage
#   make lint       static checks only (ruff + naming + import topology)
#   make e2e        live tests: real LLM / real container (needs credentials)
#   make build      wheels + sdists for both packages into dist/
#   make docs       the VitePress site, live-reloading

PY ?= uv run python

.DEFAULT_GOAL := help
.PHONY: help install check test lint e2e build docs

help:
	@echo "noeta — library repo (packages/noeta-runtime + packages/noeta-sdk)"
	@echo ""
	@echo "  make install    first time: uv sync (kernel + dev group)"
	@echo "  make check      the local CI gate: ruff, root pytest+coverage, mypy, naming + import lints"
	@echo "  make test       run the test suite with coverage"
	@echo "  make lint       static checks only: ruff + naming + import topology"
	@echo "  make e2e        live tests: real LLM / real container (needs credentials)"
	@echo "  make build      wheels + sdists for both packages into dist/"
	@echo "  make docs       the VitePress site, live-reloading"

## first-time install: workspace sync (kernel + dev group)
install:
	uv sync

## run the test suite (root pytest with coverage)
test:
	uv run pytest -n auto --cov=noeta --cov-report=term

## static checks only (fast; no tests)
lint:
	uv run ruff check packages tests scripts
	uv run python scripts/lint-naming.py
	uv run lint-imports --config .importlinter

## live tests against a real LLM backend / sandbox container (opt-in, never in check)
e2e:
	uv run pytest -m live

## build both distributions the way release.yml does
build:
	uv build --all-packages --wheel
	uv build --package noeta-runtime --sdist
	uv build --package noeta-sdk --sdist

## the docs site, live-reloading (npm ci first)
docs:
	npm run docs:dev

## every automated gate — CI's test job runs this same target. CI-only on top:
## the Postgres params of the storage contract tests (skipped unless
## NOETA_TEST_POSTGRES_DSN points at a live server), the Python 3.11-3.13
## matrix, and the fresh-venv install smoke.
check:
	uv run ruff check packages tests scripts
	uv run pytest -n auto --cov=noeta --cov-report=term --cov-fail-under=85
	uv run mypy
	uv run python scripts/lint-naming.py
	uv run lint-imports --config .importlinter
	bash scripts/check-docs.sh
