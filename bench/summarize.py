#!/usr/bin/env python3
"""Summarize harbor benchmark job(s) into a score — pass / fail / error, both
denominators, and (for TB) a per-difficulty split.

Why this exists: harbor writes a `result.json` per job. This reads one (or
several, later jobs overriding earlier verdicts per instance) and reports
errors separately, never silently counted as passes.

Usage:
    # one job
    bench/summarize.py bench/jobs/2026-08-09__11-12-24
    # point at a TB dataset dir to get the per-difficulty split
    bench/summarize.py --dataset bench/datasets/terminal-bench-2-1 bench/jobs/<job>...

Reads only result.json (the stable, post-run file); does not need the trial
subdirs, which harbor's --delete may have cleaned.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _instance(task_key: str) -> str:
    """harbor task ids look like `<name>__<trialhash>` or
    `swe-bench/<inst>__<hash>`. Strip the org prefix and the trial suffix to get
    a stable instance name across jobs."""
    name = task_key.split("/", 1)[1] if "/" in task_key else task_key
    # trial hash is the last __<alnum> segment
    if "__" in name:
        name = name.rsplit("__", 1)[0]
    return name


def parse_job(path: str) -> dict[str, tuple[str, str]]:
    """Return {instance: (verdict, detail)} where verdict is pass|fail|error."""
    rp = os.path.join(path, "result.json")
    with open(rp) as f:
        r = json.load(f)
    out: dict[str, tuple[str, str]] = {}
    for _k, v in r.get("stats", {}).get("evals", {}).items():
        for reward, tasks in v.get("reward_stats", {}).get("reward", {}).items():
            verdict = "pass" if float(reward) > 0 else "fail"
            for t in tasks:
                out[_instance(t)] = (verdict, "")
        for etype, tasks in v.get("exception_stats", {}).items():
            for t in tasks:
                out[_instance(t)] = ("error", etype)
    return out


def difficulty_of(dataset_dir: str, inst: str) -> str:
    tp = os.path.join(dataset_dir, inst, "task.toml")
    if not os.path.isfile(tp):
        return "?"
    try:
        import tomllib
    except ModuleNotFoundError:  # py<3.11
        return "?"
    with open(tp, "rb") as f:
        t = tomllib.load(f)
    return t.get("metadata", {}).get("difficulty") or t.get("difficulty") or "?"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize harbor benchmark job(s).")
    ap.add_argument("jobs", nargs="+", help="job dir(s); later overrides earlier per instance")
    ap.add_argument("--dataset", help="dataset dir for a per-difficulty split (TB only)")
    args = ap.parse_args(argv)

    merged: dict[str, tuple[str, str]] = {}
    for j in args.jobs:
        merged.update(parse_job(j))  # later jobs win

    passed = sorted(i for i, (v, _) in merged.items() if v == "pass")
    failed = sorted(i for i, (v, _) in merged.items() if v == "fail")
    errored = sorted((i, d) for i, (v, d) in merged.items() if v == "error")

    total = len(merged)
    scored = len(passed) + len(failed)

    print(f"jobs merged: {len(args.jobs)}  instances: {total}")
    print(f"  PASSED  ({len(passed)}): {', '.join(passed) if passed else '-'}")
    print(f"  FAILED  ({len(failed)}): {', '.join(failed) if failed else '-'}")
    if errored:
        print(f"  ERRORED ({len(errored)}):")
        for i, d in errored:
            print(f"      {i}  [{d}]")
    print()
    if total:
        print(f"strict (errored = fail):   {len(passed)}/{total} = {100*len(passed)/total:.1f}%")
    if scored:
        print(f"fair (exclude {len(errored)} errored): {len(passed)}/{scored} = {100*len(passed)/scored:.1f}%")

    if args.dataset:
        from collections import defaultdict
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # diff -> [pass,fail,err]
        idx = {"pass": 0, "fail": 1, "error": 2}
        for inst, (v, _) in merged.items():
            agg[difficulty_of(args.dataset, inst)][idx[v]] += 1
        print("\nby difficulty (pass / fail / err):")
        for d in ("easy", "medium", "hard", "?"):
            if d in agg:
                p, f, e = agg[d]
                tot = p + f + e
                print(f"  {d:6}: {p}/{tot} = {100*p/tot:.0f}%  (fail {f}, err {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
