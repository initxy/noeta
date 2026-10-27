#!/usr/bin/env python3
"""Merge the sample40 main run with its concurrency-1 rerun into a final score.

Two rules the generic summarize.py gets wrong for this workflow:

1. A verifier reward is authoritative. `reward_stats` is filled from the
   verifier; `exception_stats` is harbor's own bookkeeping. A task can finish
   with every test green (reward 1.0) yet be marked `AgentTimeout` because the
   agent process exited untidily. Reward wins; the exception is noted only for
   an instance that has NO verifier reward.

2. The rerun overrides the main run per instance, but only for the instances it
   actually contains. The main run is xhigh at concurrency 2 (resource
   contention inflates failures); the rerun is the same model/effort at
   concurrency 1 with the standard time wall — the clean verdict.

Usage:
    bench/merge_sample40.py <main_job> <rerun_job> [--dataset DIR]
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict


def instance(task_key: str) -> str:
    name = task_key.split("/", 1)[1] if "/" in task_key else task_key
    return name.rsplit("__", 1)[0] if "__" in name else name


def parse_job(path: str) -> dict[str, dict]:
    """{inst: {verdict: pass|fail|error, reward: float|None, exc: str}}.

    Reward first; an exception only fills an instance with no reward."""
    with open(os.path.join(path, "result.json")) as f:
        r = json.load(f)
    out: dict[str, dict] = {}
    for v in r.get("stats", {}).get("evals", {}).values():
        for reward, tasks in v.get("reward_stats", {}).get("reward", {}).items():
            for t in tasks:
                out[instance(t)] = {
                    "verdict": "pass" if float(reward) > 0 else "fail",
                    "reward": float(reward),
                    "exc": None,
                }
        for etype, tasks in v.get("exception_stats", {}).items():
            for t in tasks:
                i = instance(t)
                if i not in out:  # reward is authoritative; don't overwrite
                    out[i] = {"verdict": "error", "reward": None, "exc": etype}
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_job")
    ap.add_argument("rerun_job")
    ap.add_argument("--dataset", default="bench/datasets/terminal-bench-2-1")
    args = ap.parse_args()

    main = parse_job(args.main_job)
    rerun = parse_job(args.rerun_job)

    merged = dict(main)
    rescued, flipped_down = [], []
    for inst, rv in rerun.items():
        old = merged.get(inst, {}).get("verdict")
        merged[inst] = rv
        if old != "pass" and rv["verdict"] == "pass":
            rescued.append(inst)
        elif old == "pass" and rv["verdict"] != "pass":
            flipped_down.append(inst)

    passed = sorted(i for i, r in merged.items() if r["verdict"] == "pass")
    failed = sorted(i for i, r in merged.items() if r["verdict"] == "fail")
    errored = sorted((i, r["exc"]) for i, r in merged.items() if r["verdict"] == "error")
    total = len(merged)

    print(f"instances: {total}   rerun covered: {len(rerun)}")
    print(f"rescued by concurrency-1 rerun ({len(rescued)}): {', '.join(sorted(rescued)) or '-'}")
    if flipped_down:
        print(f"!! flipped DOWN in rerun ({len(flipped_down)}): {', '.join(flipped_down)}")
    print(f"\nFINAL PASSED ({len(passed)}): {', '.join(passed)}")
    print(f"FINAL FAILED ({len(failed)}): {', '.join(failed)}")
    if errored:
        print(f"FINAL ERRORED ({len(errored)}): " + ", ".join(f"{i}[{e}]" for i, e in errored))
    print()
    print(f"strict (errored=fail): {len(passed)}/{total} = {100*len(passed)/total:.1f}%")
    scored = len(passed) + len(failed)
    if scored:
        print(f"fair  (exclude {len(errored)} err): {len(passed)}/{scored} = {100*len(passed)/scored:.1f}%")

    if args.dataset and os.path.isdir(args.dataset):
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for inst, r in merged.items():
            d = difficulty_of(args.dataset, inst)
            agg[d][{"pass": 0, "fail": 1, "error": 2}[r["verdict"]]] += 1
        print("\nby difficulty (pass/total):")
        for d in ("easy", "medium", "hard", "?"):
            if d in agg:
                p, f, e = agg[d]
                print(f"  {d:6}: {p}/{p+f+e} = {100*p/(p+f+e):.0f}%  (fail {f}, err {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
