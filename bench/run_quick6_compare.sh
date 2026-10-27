#!/usr/bin/env bash
# TB2.1 quick6 across several models: same six tasks, effort pinned high,
# concurrency 2. Runs are sequential so two CPU-heavy runs don't steal from
# each other; per-model job dirs (bench/jobs/quick6-<tag>) keep attribution
# trivial. <tag> is the model id's last `/`-separated segment.
#
#   bench/run_quick6_compare.sh <model> [<model> ...]
#
# NOETA_PROXY / NOETA_EFFORT / NOETA_CONCURRENCY are read from the environment
# (see bench/README.md); effort and concurrency default to high / 2 here.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <model> [<model> ...]" >&2
  exit 2
fi

export NOETA_EFFORT="${NOETA_EFFORT:-high}"
export NOETA_CONCURRENCY="${NOETA_CONCURRENCY:-2}"

for M in "$@"; do
  tag="${M##*/}"
  echo "================ $M ($(date +%H:%M:%S)) ================"
  NOETA_MODEL="$M" NOETA_JOBS_DIR="bench/jobs/quick6-$tag" \
    bench/run_benchmark.sh tb-quick6 2>&1
  echo "================ done $M ($(date +%H:%M:%S)) ================"
done
echo "ALL_QUICK6_RUNS_FINISHED"
