"""``python -m evals`` — run the prompt-regression scenarios and compare to baseline.

    make eval             run every scenario N times, print the table, diff the baseline
    make eval-baseline    same run, then rewrite evals/baseline.json from it

Credentials: the repo-root ``.env`` read by ``tests/_live_env.py``
(``NOETA_LIVE_BASE_URL`` / ``NOETA_LIVE_API_KEY`` / ``NOETA_LIVE_MODEL``).
Exit status 1 when a scenario (or the overall mean) falls below its baseline by
more than the tolerance recorded in the baseline file.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from evals.harness import PROVIDERS, EvalConfig, Judge, time_trial
from evals.scenarios import SCENARIOS
from tests import _live_env

_HERE = Path(__file__).resolve().parent
BASELINE = _HERE / "baseline.json"
RESULTS_DIR = _HERE / "results"

#: Used only when the baseline file has none yet; the committed baseline
#: carries the measured values.
_DEFAULT_TOLERANCE: dict[str, Any] = {"scenario": 0.34, "overall": 0.05}


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=_HERE
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "packages"], capture_output=True, text=True, cwd=_HERE
        )
        return out.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


def _parse(argv: Optional[list[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m evals", description=__doc__.split("\n")[0])
    ap.add_argument("--trials", type=int, default=3, help="runs per scenario (default 3; make eval-baseline uses 9)")
    ap.add_argument("--concurrency", type=int, default=14, help="trials in flight at once")
    ap.add_argument("--only", nargs="*", default=None, help="scenario names (default: all)")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="anthropic")
    ap.add_argument("--model", default=None, help="default: NOETA_LIVE_MODEL")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--write-baseline", action="store_true", help="rewrite the baseline from this run")
    ap.add_argument(
        "--system-prompt-file",
        type=Path,
        default=None,
        help="replace main's system prompt with this file (for checking that the eval catches a degraded prompt)",
    )
    ap.add_argument("--out", type=Path, default=None, help="results JSON path (default: evals/results/<utc>.json)")
    return ap.parse_args(argv)


def _summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by.setdefault(r["scenario"], []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for sc in SCENARIOS:
        rs = sorted(by.get(sc.name, []), key=lambda r: r["trial"])
        if not rs:
            continue
        scores = [float(r["score"]) for r in rs]
        out[sc.name] = {
            "mean": round(statistics.fmean(scores), 4),
            "stdev": round(statistics.pstdev(scores), 4),
            "scores": scores,
            "errors": sum(1 for r in rs if r.get("error")),
        }
    return out


def _usage_totals(records: list[dict[str, Any]], judge: Judge) -> dict[str, Any]:
    tot: dict[str, Any] = {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
                           "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for r in records:
        for k in tot:
            tot[k] += r.get("usage", {}).get(k, 0)
    tot["judge_requests"] = judge.requests
    tot["judge_input_tokens"] = judge.input_tokens
    tot["judge_output_tokens"] = judge.output_tokens
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    return tot


def _compare(
    summary: dict[str, dict[str, Any]], overall: float, baseline: Optional[dict[str, Any]]
) -> tuple[list[str], dict[str, str]]:
    """Regression lines plus a per-scenario verdict column."""
    verdict: dict[str, str] = {}
    regressions: list[str] = []
    if baseline is None:
        return regressions, verdict
    tol = baseline.get("tolerance", _DEFAULT_TOLERANCE)
    base = baseline.get("scenarios", {})
    for name, s in summary.items():
        if name not in base:
            verdict[name] = "new"
            continue
        delta = s["mean"] - base[name]["mean"]
        if delta < -tol.get("per_scenario", {}).get(name, tol["scenario"]):
            verdict[name] = f"REGRESSED {delta:+.2f}"
            regressions.append(f"{name}: {base[name]['mean']:.2f} -> {s['mean']:.2f}")
        else:
            verdict[name] = f"{delta:+.2f}"
    if set(summary) == set(base):
        d = overall - baseline["overall"]
        verdict["__overall__"] = f"{d:+.3f}"
        if d < -tol["overall"]:
            verdict["__overall__"] = f"REGRESSED {d:+.3f}"
            regressions.append(f"overall: {baseline['overall']:.3f} -> {overall:.3f}")
    return regressions, verdict


def _table(summary: dict[str, dict[str, Any]], overall: float, verdict: dict[str, str],
           baseline: Optional[dict[str, Any]]) -> str:
    base = (baseline or {}).get("scenarios", {})
    rows = [("scenario", "scores", "mean", "base", "vs base")]
    for name, s in summary.items():
        b = base.get(name, {}).get("mean")
        rows.append((
            name,
            " ".join(f"{x:.2f}" for x in s["scores"]) + (f"  ({s['errors']} err)" if s["errors"] else ""),
            f"{s['mean']:.2f}",
            "-" if b is None else f"{b:.2f}",
            verdict.get(name, "-"),
        ))
    bo = (baseline or {}).get("overall")
    rows.append(("OVERALL", "", f"{overall:.3f}", "-" if bo is None else f"{bo:.3f}",
                 verdict.get("__overall__", "-")))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse(argv)
    model = args.model or _live_env.live_model()
    if not (_live_env.have_live_env() and model):
        print("evals: need NOETA_LIVE_BASE_URL / NOETA_LIVE_API_KEY / NOETA_LIVE_MODEL (repo-root .env)")
        return 2
    prompt = args.system_prompt_file.read_text(encoding="utf-8") if args.system_prompt_file else None
    cfg = EvalConfig(model=model, provider=args.provider, system_prompt=prompt)
    chosen = [s for s in SCENARIOS if args.only is None or s.name in args.only]
    unknown = set(args.only or ()) - {s.name for s in SCENARIOS}
    if unknown:
        print(f"evals: unknown scenario(s): {', '.join(sorted(unknown))}")
        return 2
    baseline = json.loads(args.baseline.read_text()) if args.baseline.is_file() else None
    if baseline and baseline.get("model") != model:
        print(f"evals: warning — baseline was recorded on {baseline.get('model')!r}, running {model!r}")

    judge = Judge(cfg)
    jobs = [(s, i) for i in range(args.trials) for s in chosen]
    started = time.monotonic()
    print(f"evals: {len(chosen)} scenarios x {args.trials} trials on {model} ({args.provider}), "
          f"concurrency {args.concurrency}", flush=True)
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(time_trial, cfg, s, judge, i) for s, i in jobs]
        for f in as_completed(futures):
            r = f.result()
            records.append(r)
            print(f"  {r['scenario']:<30} #{r['trial']}  {r['score']:.2f}  {r['seconds']:>6.1f}s"
                  + (f"  [{r['error'][:80]}]" if r.get("error") else ""), flush=True)
    wall = round(time.monotonic() - started, 1)

    summary = _summarize(records)
    overall = round(statistics.fmean(s["mean"] for s in summary.values()), 4)
    regressions, verdict = _compare(summary, overall, None if args.write_baseline else baseline)
    usage = _usage_totals(records, judge)
    result = {
        "recorded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": _git_rev(),
        "model": model,
        "provider": args.provider,
        "trials": args.trials,
        "system_prompt_override": str(args.system_prompt_file) if args.system_prompt_file else None,
        "wall_seconds": wall,
        "usage": usage,
        "overall": overall,
        "scenarios": summary,
        "records": records,
    }
    out = args.out or RESULTS_DIR / f"{result['recorded'].replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")

    print()
    print(_table(summary, overall, verdict, None if args.write_baseline else baseline))
    print()
    print(f"wall {wall}s; {usage['requests']} agent requests, "
          f"{usage['input_tokens']:,} input tokens "
          f"({usage['cache_read_tokens']:,} cached), {usage['output_tokens']:,} output; "
          f"judge {usage['judge_requests']} requests; catalog cost ${usage['cost_usd']}")
    print(f"results: {out}")

    if args.write_baseline:
        if args.only is not None or args.system_prompt_file is not None:
            print("evals: refusing to write a baseline from a partial or prompt-overridden run")
            return 2
        tol = (baseline or {}).get("tolerance", _DEFAULT_TOLERANCE)
        args.baseline.write_text(json.dumps({
            "recorded": result["recorded"],
            "git": result["git"],
            "model": model,
            "provider": args.provider,
            "trials": args.trials,
            "tolerance": tol,
            "overall": overall,
            "scenarios": {k: {"mean": v["mean"], "scores": v["scores"]} for k, v in summary.items()},
        }, indent=2) + "\n")
        print(f"baseline written: {args.baseline}")
        return 0
    if regressions:
        print("REGRESSED:\n  " + "\n  ".join(regressions))
        return 1
    print("no regression" if baseline else "no baseline to compare against (make eval-baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
