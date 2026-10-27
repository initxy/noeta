#!/usr/bin/env python3
"""Per-instance reward table across quick6 model jobs.

Reads each model's newest result.json under bench/jobs/quick6-<tag>/ and prints
a task × model grid (reward, or the exception short name when the trial
errored).

    bench/quick6_table.py <tag>[=<label>][+<rerun_dir>...] ...

`<tag>` is the job-dir suffix `run_quick6_compare.sh` wrote; each `+<rerun_dir>`
(a dir under bench/jobs/) is overlaid on top, oldest job first. With no
arguments every bench/jobs/quick6-*/ dir is a column, labelled by its tag.
"""
from __future__ import annotations

import glob
import json
import os
import sys

TASKS = [
    "fix-git",
    "nginx-request-logging",
    "log-summary-date-ranges",
    "sqlite-db-truncate",
    "overfull-hbox",
    "adaptive-rejection-sampler",
]


def latest_result(tag: str) -> dict | None:
    jobs = sorted(glob.glob(f"bench/jobs/quick6-{tag}/*/result.json"))
    return json.load(open(jobs[-1])) if jobs else None


def verdicts(d: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    ev = d["stats"]["evals"]
    for _, v in ev.items():
        for reward, names in v["reward_stats"]["reward"].items():
            for n in names:
                out[n.split("__")[0]] = str(reward)
        for exc, names in v["exception_stats"].items():
            for n in names:
                out[n.split("__")[0]] = exc.replace("Error", "Err")[:12]
    return out


def merged_verdicts(tag: str, rerun_dirs: list[str]) -> dict[str, str]:
    """Main job first, then every rerun job oldest→newest overlaid, so a
    concurrency-1 rerun that passes replaces the main run's timeout verdict."""
    out: dict[str, str] = {}
    main = latest_result(tag)
    if main:
        out.update(verdicts(main))
    for rd in rerun_dirs:
        jobs = sorted(glob.glob(f"bench/jobs/{rd}/*/result.json"))
        for p in jobs:
            out.update(verdicts(json.load(open(p))))
    return out


def parse_args(argv: list[str]) -> list[tuple[str, str, list[str]]]:
    if not argv:
        dirs = sorted(glob.glob("bench/jobs/quick6-*/"))
        argv = [os.path.basename(d.rstrip("/")).removeprefix("quick6-") for d in dirs]
    out = []
    for arg in argv:
        head, *reruns = arg.split("+")
        tag, _, label = head.partition("=")
        out.append((tag, label or tag, reruns))
    return out


MODELS = parse_args(sys.argv[1:])
cols = {tag: merged_verdicts(tag, reruns) for tag, _, reruns in MODELS}

labels = [lbl for _, lbl, _ in MODELS]
w = max(len(l) for l in labels)
print(f"{'task':32s} " + " ".join(f"{l:>{w}}" for l in labels))
print("-" * (34 + (w + 1) * len(labels)))
for t in TASKS:
    cells = [cols[tag].get(t, "—") for tag, _, _ in MODELS]
    print(f"{t:32s} " + " ".join(f"{c:>{w}}" for c in cells))

print()
for tag, lbl, _ in MODELS:
    v = cols[tag]
    done = sum(1 for t in TASKS if t in v)
    passed = sum(1 for t in TASKS if v.get(t) in ("1", "1.0"))
    print(f"{lbl:>{w}}: {passed}/{done} passed")
