# noeta — developer convenience wrapper.
#
#   make install    first time: uv sync with every extra
#   make check      the local CI gate (mirrors .github/workflows/ci.yml)
#   make test       the test suite with coverage
#   make lint       static checks only (ruff, mypy, naming + import layering)
#   make example    the example plugin, installed as its own package, and its tests
#   make tui        the terminal UI's tests (tui/, its own package)
#   make web        the web app: install, typecheck, unit and e2e tests (web/)
#   make desktop    the desktop app: install, typecheck, unit and e2e tests (desktop/)
#   make frontends  every front end's tests (tui/, web/, desktop/)
#   make docs       build the docs site (docs/, VitePress); make docs-dev serves it live
#   make e2e        live tests: real LLM / real container (needs credentials)
#   make eval       prompt-regression eval vs evals/baseline.json (needs credentials)
#   make parity     replay Claude Code's recorded scenarios against noeta, report every difference
#   make cluster    worker processes on Postgres under random kills (needs docker or a DSN)
#   make build      wheel + sdist into dist/

.DEFAULT_GOAL := help
.PHONY: help install check test lint example tui tui-screenshots web desktop frontends docs docs-dev e2e eval eval-baseline \
	parity cluster build

help:
	@echo "noeta"
	@echo ""
	@echo "  make install    first time: uv sync with every extra"
	@echo "  make check      the local CI gate: lint + tests with coverage + doc hygiene"
	@echo "  make test       the test suite with coverage"
	@echo "  make lint       static checks only: ruff, mypy, naming + import layering"
	@echo "  make example    install examples/hello-plugin as a package and run its tests"
	@echo "  make tui        the terminal UI's tests (tui/), with coverage"
	@echo "  make tui-screenshots  rewrite tui/screenshots/*.svg"
	@echo "  make web        web/: install, typecheck, unit tests, e2e against a real noeta serve"
	@echo "  make desktop    desktop/: install, typecheck, unit tests, e2e of the built app (xvfb-run)"
	@echo "  make frontends  every front end's tests (tui/, web/, desktop/)"
	@echo "  make docs       build the docs site into docs/.vitepress/dist (docs-dev: live preview)"
	@echo "  make e2e        live tests: real LLM / real container (needs credentials)"
	@echo "  make eval       prompt-regression eval vs evals/baseline.json (needs credentials)"
	@echo "  make eval-baseline  9-trial run, then rewrite evals/baseline.json"
	@echo "  make parity     replay Claude Code's recorded scenarios, write parity/report.md"
	@echo "  make cluster    worker processes on Postgres under random kills (docker or a DSN)"
	@echo "  make build      wheel + sdist into dist/"

install:
	uv sync --all-extras

test:
	uv run pytest -n auto --cov=noeta --cov-report=term

lint:
	uv run ruff check noeta tests evals bench scripts examples tui
	uv run mypy
	uv run python scripts/lint-naming.py
	uv run python scripts/lint-layout.py
	uv run lint-imports

## a third-party plugin end to end: built and installed from examples/hello-plugin
## (found through its entry point, nothing patched), in a throwaway environment;
## then the single-file plugins of examples/ (docs/guides/plugins.md), loaded as plain files
PLUGIN_FILE_EXAMPLES = examples/run-tests/tests examples/chat-bridge/tests \
	examples/chat-approvals/tests examples/house-style/tests
example:
	uv run --with-editable ./examples/hello-plugin python -m pytest -q -p no:cacheprovider examples/hello-plugin/tests
	uv run pytest -q -p no:cacheprovider $(PLUGIN_FILE_EXAMPLES)

## the terminal UI, its own package (tui/, a uv workspace member): Textual pilot tests
tui:
	cd tui && uv run pytest -n auto -q --cov=noeta_tui --cov-report=term:skip-covered --cov-fail-under=85

## the screenshots tui/screenshots/README.md compares with Claude Code's
tui-screenshots:
	cd tui && rm -rf /tmp/noeta-demo && NOETA_TUI_SCREENSHOTS=1 uv run pytest -q --basetemp=/tmp/noeta-demo tests/test_screenshots.py

## every front end's own tests
frontends: tui web desktop

## the docs site: diagrams on the shared style, then a VitePress build (dead links fail it)
docs:
	cd docs && npm ci && npm run build

docs-dev:
	cd docs && npm ci && npm run dev

## the web app: typecheck, unit tests, Playwright against a real noeta serve
web:
	cd web && npm ci && npm run typecheck && npm test && npm run e2e

## the desktop app over the web build: typecheck, unit tests, Playwright's Electron
## driver under xvfb-run against the noeta serve it starts
desktop:
	cd web && npm ci && npm run build
	cd desktop && npm ci && npm run typecheck && npm test && npm run e2e

## live tests against a real LLM backend / sandbox container (opt-in, never in check)
e2e:
	uv run pytest -m live

## prompt-regression eval against a real model (opt-in, never in check);
## exits 1 when a scenario falls below evals/baseline.json by more than its tolerance
## the model: EVAL_MODEL if given, else NOETA_LIVE_MODEL from the git-ignored .env
## (the baseline is recorded on DeepSeek V4.1 Flash)
eval:
	uv run python -m evals $(if $(EVAL_MODEL),--model $(EVAL_MODEL))

## rewrite evals/baseline.json from a 9-trial run (a steadier mean than eval's 3)
eval-baseline:
	uv run python -m evals $(if $(EVAL_MODEL),--model $(EVAL_MODEL)) --trials 9 --write-baseline

## noeta replayed through Claude Code's recorded scenarios (scripts/parity/README.md),
## offline; writes parity/report.md and exits 1 while any mismatch is left (never in check).
## First, an advisory report of model-visible texts that repeat Claude Code's
## recorded text (scripts/parity/own_words.py; never fails, ADR-020)
parity:
	uv run python -m scripts.parity.own_words
	uv run python -m scripts.parity.replay

## several worker processes on one Postgres (a throwaway postgres:16 container,
## or NOETA_TEST_POSTGRES_DSN) under random SIGKILL / SIGSTOP, offline with a
## scripted provider (tests/cluster); NOETA_CLUSTER_SEED replays a run (never in check).
## `python -m pytest` so the run's interpreter sees the scripted package; a skipped
## cluster test fails the run (NOETA_CLUSTER_REQUIRED)
cluster:
	NOETA_CLUSTER_REQUIRED=1 uv run --all-extras --with-editable ./tests/cluster/scripted python -m pytest -m cluster -p no:cacheprovider -s -v tests/cluster

build:
	uv build

## the front ends, each its own project with its own test command (never in check:
## it needs node and a browser). web/: the e2e test runs a real noeta serve on the
## offline scripted provider in web/e2e/provider

## every automated gate — CI's test job runs this same target. CI-only on top:
## the Postgres params of the store contract tests (skipped unless
## NOETA_TEST_POSTGRES_DSN points at a live server) and the Python 3.11-3.13 matrix.
check:
	uv sync -q --all-extras
	$(MAKE) --no-print-directory lint
	uv run pytest -n auto -q --cov=noeta --cov-report=term:skip-covered --cov-fail-under=85
	$(MAKE) --no-print-directory example
	$(MAKE) --no-print-directory tui
	bash scripts/check-docs.sh
