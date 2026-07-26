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

PY ?= uv run python

.DEFAULT_GOAL := help
.PHONY: help install check test lint

help:
	@echo "noeta — library repo (packages/noeta-runtime + packages/noeta-sdk)"
	@echo ""
	@echo "  make install    first time: uv sync (kernel + dev group)"
	@echo "  make check      the local CI gate: root pytest+coverage, mypy, naming + import lints"
	@echo "  make test       run the test suite with coverage"
	@echo "  make lint       static checks only: ruff + naming + import topology"

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

## the local CI gate — mirrors .github/workflows/ci.yml minus what needs CI infrastructure.
## CI-only steps (expected to be absent locally): the Postgres storage contract tests
## (skipped unless NOETA_TEST_POSTGRES_DSN points at a live server) and the
## fresh-venv install smoke.
check:
	uv run pytest -n auto --cov=noeta --cov-report=term --cov-fail-under=85
	MYPYPATH=packages/noeta-runtime uv run mypy --strict \
	  --namespace-packages --explicit-package-bases \
	  packages/noeta-runtime/noeta/protocols
	uv run python scripts/lint-naming.py
	uv run lint-imports --config .importlinter
