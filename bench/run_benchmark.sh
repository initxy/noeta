#!/usr/bin/env bash
# Run Noeta against a harbor benchmark (Terminal-Bench 2.0 / 2.1 or SWE-bench).
#
# This is the one command that turns "the adapter exists" into "a score comes
# out". It does NOT run inside `make check`: it needs Docker, the `harbor` CLI,
# and real gateway credentials, and it spends real tokens. Run it on a machine
# you control, with an LLM budget you are willing to spend.
#
# ── Quick start ───────────────────────────────────────────────────────────────
# The script sources .env and sets the sandbox plumbing (wheel, proxy, model
# catalogue) itself, so a run is just the model + effort you want:
#
#     NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh \
#       bench/run_benchmark.sh tb21-sample40
#
# Everything below has a sane default; override any of it inline.
#
# ── Modes ─────────────────────────────────────────────────────────────────────
#   smoke-tb        1 TB2.1 task         — prove the loop (cheap)
#   smoke-swe       1 SWE-bench task     — prove the loop (cheap)
#   tb-quick6       TB2.1 6-task quick model-comparison set (see QUICK6 below)
#   tb21-sample40   TB2.1 pinned 40-task stratified sample
#   tb21-full       TB2.1 full 89-set
#   tb3-full        Terminal-Bench 3 full 75-set (harder terminal board)
#   aider-full      Aider Polyglot full 225-set (multilingual code editing)
#   swe-15          SWE-bench Verified pinned 15-instance subset
#   swe-60          SWE-bench Verified pinned 62-instance subset (proportional)
#   swe-full        SWE-bench Verified FULL 500-instance set (very large)
#   swe-pro-full    SWE-bench Pro full 731-set (anti-contamination, large)
#   swe-subset      SWE-bench Verified, SWE_INCLUDE_GLOBS or first-N
#
# ── Knobs (all optional; env vars) ────────────────────────────────────────────
#   NOETA_MODEL       model id passed to -m           (default: opus4.8)
#   NOETA_EFFORT      reasoning effort: low|medium|high|xhigh|max
#                     (default: high. The public TB2.1 board's leaders use
#                      xhigh/max; match that for a leaderboard-comparable run.)
#   NOETA_CONCURRENCY --n-concurrent                  (default: 3)
#                     Keep ≤2 for SWE-bench: its images need a private 3.12 built
#                     with uv, and parallel toolchain fetches trip setup timeouts.
#   NOETA_SETUP_TIMEOUT_MULT  agent-setup timeout multiplier (default: 3; use 5
#                     for SWE-bench so the uv 3.12 provisioning fits).
#   NOETA_WHEEL       local wheel to install in-sandbox (default: newest dist/*.whl)
#   NOETA_PROXY       container-reachable proxy        (default: from env/.env)
#   NOETA_MODELS_CONFIG  model catalogue to upload     (default: ./models.json)
#   NOETA_JOBS_DIR    where harbor writes results      (default: bench/jobs)
#   NOETA_ENV_FILE    dotenv with LLM_BASE_URL/LLM_API_KEY (default: ./.env)
#   NOETA_PI_EXPORT   path to the pi usage exporter; when present and executable,
#                     the run's per-trial token usage is exported to kaboo's pi
#                     source at the end (best-effort, never fails the run).
#                     (default: ~/.pi/exporter/pi_export.py; empty to skip)
#   TB21_DATASET / SWE_DATASET   dataset name@version overrides
#   SWE_INCLUDE_GLOBS / SWE_SUBSET_SIZE       swe-subset selection
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Load gateway credentials from .env unless already in the environment ──────
# So a bare `NOETA_MODEL=… NOETA_EFFORT=… bench/run_benchmark.sh <mode>` works
# without the caller re-exporting LLM_BASE_URL / LLM_API_KEY every time.
ENV_FILE="${NOETA_ENV_FILE:-${REPO_ROOT}/.env}"
if [ -z "${NOETA_BASE_URL:-${LLM_BASE_URL:-}}" ] && [ -f "${ENV_FILE}" ]; then
  set -a; . "${ENV_FILE}"; set +a
fi

MODEL="${NOETA_MODEL:-opus4.8}"
EFFORT="${NOETA_EFFORT:-high}"
AGENT="bench.harbor_adapter:Noeta"
CONCURRENCY="${NOETA_CONCURRENCY:-3}"
SETUP_TIMEOUT_MULT="${NOETA_SETUP_TIMEOUT_MULT:-3}"

# harbor registry dataset ids (org/dataset), verified via `harbor datasets
# download`. TB2.1 (terminal-bench-2-1) is the current public leaderboard and
# the TB target. swe-bench-verified is the 500-instance Verified set.
TB21_DATASET="${TB21_DATASET:-terminal-bench/terminal-bench-2-1}"
SWE_DATASET="${SWE_DATASET:-swe-bench/swe-bench-verified}"
# Additional public benchmarks, all in the harbor registry (verified via
# `harbor datasets download`): multilingual code editing, an anti-contamination
# hard SWE set, and the harder terminal board.
TB3_DATASET="${TB3_DATASET:-terminal-bench/terminal-bench-3}"
AIDER_DATASET="${AIDER_DATASET:-aider/aider-polyglot}"
SWEPRO_DATASET="${SWEPRO_DATASET:-scale-ai/swe-bench-pro}"

JOBS_DIR="${NOETA_JOBS_DIR:-${REPO_ROOT}/bench/jobs}"

# ── Sandbox plumbing the adapter reads (export so harbor's child sees them) ───
# Wheel: newest dist/*.whl unless NOETA_WHEEL is set. The adapter uploads it and
# pip-installs it in the sandbox (build it with `uv build --wheel`).
if [ -z "${NOETA_WHEEL:-}" ]; then
  _wheel="$(ls -t "${REPO_ROOT}"/dist/noeta-1*.whl 2>/dev/null | head -1 || true)"
  [ -n "${_wheel}" ] && export NOETA_WHEEL="${_wheel}"
fi
# Model catalogue: upload ./models.json so the gateway model runs at its real
# 128k output instead of the sandbox fallback's 16k. Only if it exists.
if [ -z "${NOETA_MODELS_CONFIG:-}" ] && [ -f "${REPO_ROOT}/models.json" ]; then
  export NOETA_MODELS_CONFIG="${REPO_ROOT}/models.json"
fi
# Effort reaches the adapter through NOETA_EFFORT; export the resolved value.
export NOETA_EFFORT="${EFFORT}"
# NOETA_PROXY (container-reachable) is passed through if the caller set it.

die() { echo "error: $*" >&2; exit 1; }

preflight() {
  command -v harbor >/dev/null 2>&1 || die "harbor CLI not found. Install: pip install harbor (or: uv tool install harbor)"
  command -v docker >/dev/null 2>&1 || die "docker not found. harbor runs each task in a container."
  docker info >/dev/null 2>&1 || die "docker daemon not reachable. Start Docker first."
  [ -n "${NOETA_BASE_URL:-${LLM_BASE_URL:-}}" ] || die "NOETA_BASE_URL (or LLM_BASE_URL) is unset (not in env, not in ${ENV_FILE})."
  [ -n "${NOETA_API_KEY:-${LLM_API_KEY:-}}" ]  || die "NOETA_API_KEY (or LLM_API_KEY) is unset (not in env, not in ${ENV_FILE})."
  [ -n "${NOETA_WHEEL:-}" ] || echo "warning: no NOETA_WHEEL and no dist/*.whl — sandbox will pip-install NOETA_AGENT_SPEC from an index." >&2
  # harbor imports the adapter as `bench.harbor_adapter`; put repo root on PYTHONPATH.
  export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
  echo "preflight ok"
  echo "  model=${MODEL}  effort=${EFFORT}  concurrency=${CONCURRENCY}  setup_timeout_mult=${SETUP_TIMEOUT_MULT}"
  echo "  wheel=${NOETA_WHEEL:-<none>}"
  echo "  models_config=${NOETA_MODELS_CONFIG:-<none>}  proxy=${NOETA_PROXY:-<none>}"
  echo "  jobs_dir=${JOBS_DIR}"
}

# run_harbor <dataset> <extra harbor args...>
run_harbor() {
  local dataset="$1"; shift
  harbor run -d "${dataset}" -a "${AGENT}" -m "${MODEL}" -o "${JOBS_DIR}" \
    --agent-setup-timeout-multiplier "${SETUP_TIMEOUT_MULT}" \
    --n-concurrent "${CONCURRENCY}" "$@"
}

# include_args <task-prefix> <name...>  → prints "-i prefix/name" per name
include_args() {
  local prefix="$1"; shift
  local out=()
  local t
  for t in "$@"; do out+=(-i "${prefix}/${t}"); done
  printf '%s\n' "${out[@]}"
}

# A small six-task set for cheap model-vs-model comparisons: four that the
# 2026-08 pinned run showed a capable model passes (fix-git / nginx / log /
# sqlite — the floor), one depth-sensitive medium (overfull-hbox, which flips
# on reasoning care: it failed under shallow effort and passed at high), and
# one genuine hard task that failed even for a strong model at high
# (adaptive-rejection-sampler). All six are light images (no multi-minute
# compilers), Python >=3.12, and verifiable. Same tasks for every model so a
# per-task pass/fail table discriminates; the point is the table, not a score.
TB_QUICK6=(
  fix-git nginx-request-logging log-summary-date-ranges sqlite-db-truncate
  overfull-hbox adaptive-rejection-sampler
)

# The pinned stratified sample (40 tasks: 4 easy + 24 medium + 12 hard), chosen
# deterministically from the 89-task set. TB2.0 and TB2.1 share these task names.
# Excluded before sampling, for environment/harness reasons — NOT agent
# capability — and kept out so the sample composition stays fixed and citable:
#   - Python <3.12 images: the adapter now provisions a private 3.12 via uv, so
#     these are runnable, but the sample predates that and stays fixed.
#   - Unscoreable: make-mips-interpreter / make-doom-for-mips / install-windows
#     (multi-hour timeouts) and polyglot-rust-c (no-verified-solution).
# Mirror this list in docs/benchmarks.md.
TB_SAMPLE40=(
  adaptive-rejection-sampler bn-fit-modify build-cython-ext build-pov-ray
  chess-best-move circuit-fibsqrt cobol-modernization compile-compcert
  count-dataset-tokens custom-memory-heap-crash distribution-search dna-assembly
  extract-elf feal-differential-cryptanalysis financial-document-processor fix-git
  fix-ocaml-gc git-leak-recovery headless-terminal large-scale-text-editing
  llm-inference-batching-scheduler log-summary-date-ranges merge-diff-arc-agi-task
  model-extraction-relu-logits nginx-request-logging overfull-hbox path-tracing
  polyglot-c-py protein-assembly prove-plus-comm pypi-server pytorch-model-recovery
  raman-fitting reshard-c4-data sanitize-git-repo sparql-university
  sqlite-db-truncate torch-tensor-parallelism tune-mjcf video-processing
)

# The pinned SWE-bench Verified subset (15 instances), one or two per repo across
# all 12 repos in Verified, so the sample is not skewed to django's 231 rows.
# harbor names each SWE-bench task `swe-bench/<instance_id>`.
SWE_SUBSET15=(
  django__django-10097 django__django-11820 django__django-13195
  sympy__sympy-11618 sympy__sympy-13877
  sphinx-doc__sphinx-10323 matplotlib__matplotlib-13989
  scikit-learn__scikit-learn-10297 pydata__xarray-3095
  astropy__astropy-12907 pytest-dev__pytest-10051
  pylint-dev__pylint-4551 psf__requests-1724
  pallets__flask-5014 mwaskom__seaborn-3069
)

# A larger pinned SWE-bench Verified subset (62 instances), proportional across
# all 12 repos so the mix mirrors the full 500 without running all of it (the
# full set is hours + heavy tokens). Deterministic stride pick per repo.
SWE_SUBSET60=(
  astropy__astropy-12907 astropy__astropy-14096 astropy__astropy-14598
  django__django-10097 django__django-11095 django__django-11179
  django__django-11333 django__django-11603 django__django-11848
  django__django-12125 django__django-12304 django__django-12754
  django__django-13089 django__django-13279 django__django-13406
  django__django-13569 django__django-13809 django__django-14007
  django__django-14155 django__django-14404 django__django-14608
  django__django-14792 django__django-15104 django__django-15315
  django__django-15525 django__django-15731 django__django-15957
  django__django-16145 django__django-16485 django__django-16631
  django__django-16899 matplotlib__matplotlib-13989 matplotlib__matplotlib-22865
  matplotlib__matplotlib-24570 matplotlib__matplotlib-25332 mwaskom__seaborn-3069
  pallets__flask-5014 psf__requests-1142 pydata__xarray-2905
  pydata__xarray-4094 pydata__xarray-6599 pylint-dev__pylint-4551
  pytest-dev__pytest-10051 pytest-dev__pytest-6202 scikit-learn__scikit-learn-10297
  scikit-learn__scikit-learn-13124 scikit-learn__scikit-learn-14087 scikit-learn__scikit-learn-25102
  sphinx-doc__sphinx-10323 sphinx-doc__sphinx-7440 sphinx-doc__sphinx-8035
  sphinx-doc__sphinx-8593 sphinx-doc__sphinx-9320 sympy__sympy-11618
  sympy__sympy-13480 sympy__sympy-13878 sympy__sympy-15599
  sympy__sympy-16886 sympy__sympy-18698 sympy__sympy-20428
  sympy__sympy-21847 sympy__sympy-23534
)

case "${1:-}" in
  smoke-tb)
    preflight
    echo "== TB2.1 smoke: 1 task (fix-git) =="
    run_harbor "${TB21_DATASET}" -i terminal-bench/fix-git
    ;;
  tb-quick6)
    preflight
    echo "== TB2.1 quick6 model-comparison set (6 tasks) =="
    mapfile -t inc < <(include_args terminal-bench "${TB_QUICK6[@]}")
    run_harbor "${TB21_DATASET}" "${inc[@]}"
    ;;
  smoke-swe)
    preflight
    echo "== SWE-bench smoke: 1 task =="
    run_harbor "${SWE_DATASET}" -l 1
    ;;
  tb21-sample40)
    preflight
    echo "== TB2.1 pinned 40-task stratified sample — spends real tokens =="
    mapfile -t inc < <(include_args terminal-bench "${TB_SAMPLE40[@]}")
    run_harbor "${TB21_DATASET}" "${inc[@]}"
    ;;
  tb21-full)
    preflight
    echo "== TB2.1 FULL 89-set — spends real tokens =="
    run_harbor "${TB21_DATASET}"
    ;;
  swe-15)
    preflight
    echo "== SWE-bench Verified pinned 15-instance subset — spends real tokens =="
    mapfile -t inc < <(include_args swe-bench "${SWE_SUBSET15[@]}")
    run_harbor "${SWE_DATASET}" "${inc[@]}"
    ;;
  swe-full)
    preflight
    echo "== SWE-bench Verified FULL 500-instance set — VERY large, hours + heavy tokens =="
    echo "   Each instance is a real repo + full test suite (verifier timeout 3000s)."
    echo "   Keep NOETA_CONCURRENCY low (2) and NOETA_SETUP_TIMEOUT_MULT=5."
    run_harbor "${SWE_DATASET}"
    ;;
  swe-60)
    preflight
    echo "== SWE-bench Verified pinned 62-instance subset (proportional across 12 repos) =="
    mapfile -t inc < <(include_args swe-bench "${SWE_SUBSET60[@]}")
    run_harbor "${SWE_DATASET}" "${inc[@]}"
    ;;
  tb3-full)
    preflight
    echo "== Terminal-Bench 3 FULL 75-set (the harder terminal board) — spends real tokens =="
    run_harbor "${TB3_DATASET}"
    ;;
  aider-full)
    preflight
    echo "== Aider Polyglot FULL 225-set (multilingual code editing) — spends real tokens =="
    run_harbor "${AIDER_DATASET}"
    ;;
  swe-pro-full)
    preflight
    echo "== SWE-bench Pro FULL 731-set (anti-contamination, enterprise-scale) — LARGE =="
    echo "   Keep NOETA_CONCURRENCY low (2) and NOETA_SETUP_TIMEOUT_MULT=5."
    run_harbor "${SWEPRO_DATASET}"
    ;;
  swe-subset)
    preflight
    echo "== SWE-bench Verified subset — spends real tokens =="
    if [ -n "${SWE_INCLUDE_GLOBS:-}" ]; then
      inc=()
      for g in ${SWE_INCLUDE_GLOBS}; do inc+=(-i "$g"); done
      run_harbor "${SWE_DATASET}" "${inc[@]}"
    else
      SIZE="${SWE_SUBSET_SIZE:-50}"
      echo "   (no SWE_INCLUDE_GLOBS; taking first ${SIZE} by -l. Prefer -i for a citable subset.)"
      run_harbor "${SWE_DATASET}" -l "${SIZE}"
    fi
    ;;
  *)
    die "unknown mode '${1:-}'. One of: smoke-tb | smoke-swe | tb21-sample40 | tb21-full | tb3-full | aider-full | swe-15 | swe-60 | swe-full | swe-pro-full | swe-subset"
    ;;
esac

echo "done — results under ${JOBS_DIR}. Summarize into docs/benchmarks.md."

# ── Export this run's token usage to kaboo's pi source (best-effort) ──────────
# The benchmark runs `noeta -p` with a throwaway sqlite store inside throwaway
# containers, so its tokens never reach an engine db — the only record is each
# trial's agent/noeta.json. The pi exporter has a --jobs-dir mode that reads
# those and appends pi-format JSONL under ~/.pi/agent/sessions/noeta-bench,
# which kaboo collects. It is incremental (a per-label state file), so this
# only ever exports trials not already reported. Kept best-effort: a missing
# exporter or an export error must never turn a green benchmark red.
PI_EXPORT="${NOETA_PI_EXPORT-${HOME}/.pi/exporter/pi_export.py}"
if [ -n "${PI_EXPORT}" ] && [ -f "${PI_EXPORT}" ]; then
  echo "exporting token usage to kaboo (pi source) ..."
  if python3 "${PI_EXPORT}" --jobs-dir "${JOBS_DIR}" --label noeta-bench; then
    :
  else
    echo "warning: pi usage export failed (benchmark results are unaffected)" >&2
  fi
fi
