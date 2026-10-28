# noeta — developer convenience wrapper.
#
#   make install    first time: uv sync with every extra
#   make check      the local CI gate (mirrors .github/workflows/ci.yml)
#   make test       the test suite with coverage
#   make lint       static checks only (ruff, mypy, naming + import layering)
#   make example    the example plugin, installed as its own package, and its tests
#   make e2e        live tests: real LLM / real container (needs credentials)
#   make eval       prompt-regression eval vs evals/baseline.json (needs credentials)
#   make parity     replay Claude Code's recorded scenarios against noeta, report every difference
#   make build      wheel + sdist into dist/

.DEFAULT_GOAL := help
.PHONY: help install check test lint example e2e eval eval-baseline parity build

help:
	@echo "noeta"
	@echo ""
	@echo "  make install    first time: uv sync with every extra"
	@echo "  make check      the local CI gate: lint + tests with coverage + doc hygiene"
	@echo "  make test       the test suite with coverage"
	@echo "  make lint       static checks only: ruff, mypy, naming + import layering"
	@echo "  make example    install examples/hello-plugin as a package and run its tests"
	@echo "  make e2e        live tests: real LLM / real container (needs credentials)"
	@echo "  make eval       prompt-regression eval vs evals/baseline.json (needs credentials)"
	@echo "  make eval-baseline  9-trial run, then rewrite evals/baseline.json"
	@echo "  make parity     replay Claude Code's recorded scenarios, write parity/report.md"
	@echo "  make build      wheel + sdist into dist/"

install:
	uv sync --all-extras

test:
	uv run pytest -n auto --cov=noeta --cov-report=term

lint:
	uv run ruff check noeta tests evals bench scripts examples
	uv run mypy
	uv run python scripts/lint-naming.py
	uv run python scripts/lint-layout.py
	uv run lint-imports

## a third-party plugin end to end: built and installed from examples/hello-plugin
## (found through its entry point, nothing patched), in a throwaway environment
example:
	uv run --with-editable ./examples/hello-plugin pytest -q -p no:cacheprovider examples/hello-plugin/tests

## live tests against a real LLM backend / sandbox container (opt-in, never in check)
e2e:
	uv run pytest -m live

## prompt-regression eval against a real model (opt-in, never in check);
## exits 1 when a scenario falls below evals/baseline.json by more than its tolerance
eval:
	uv run python -m evals

## rewrite evals/baseline.json from a 9-trial run (a steadier mean than eval's 3)
eval-baseline:
	uv run python -m evals --trials 9 --write-baseline

## noeta replayed through Claude Code's recorded scenarios (scripts/parity/README.md),
## offline; writes parity/report.md and exits 1 while any mismatch is left (never in check)
parity:
	uv run python -m scripts.parity.replay

build:
	uv build

## every automated gate — CI's test job runs this same target. CI-only on top:
## the Postgres params of the store contract tests (skipped unless
## NOETA_TEST_POSTGRES_DSN points at a live server) and the Python 3.11-3.13 matrix.
check:
	uv sync -q --all-extras
	$(MAKE) --no-print-directory lint
	uv run pytest -n auto -q --cov=noeta --cov-report=term:skip-covered --cov-fail-under=85
	$(MAKE) --no-print-directory example
	bash scripts/check-docs.sh
