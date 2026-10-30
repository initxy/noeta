"""``bench/compare.py``: the summary's outcomes and arithmetic, the dataset
mirror, key redaction, and what the leftover cleanup counts as this run's —
offline, on synthetic job directories, with docker stubbed."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bench_compare", ROOT / "bench" / "compare.py")
assert _spec and _spec.loader
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)

MODEL = compare.DEFAULT_MODEL


def trial(job: Path, name: str, agent: str, task: str, *, reward: float | None, error: str | None = None,
          tokens: tuple[int, int, int] | None = (1_000_000, 900_000, 10_000),
          start: str = "2026-10-31T12:00:00+00:00", end: str = "2026-10-31T13:00:00+00:00") -> None:
    d = job / name
    d.mkdir(parents=True)
    result: dict[str, Any] = {
        "trial_name": name, "task_name": f"org/{task}", "agent_info": {"name": agent},
        "started_at": start, "finished_at": end,
        "agent_execution": {"started_at": start, "finished_at": end},
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        "exception_info": error and {"exception_type": error},
        "agent_result": tokens and {"n_input_tokens": tokens[0], "n_cache_tokens": tokens[1],
                                    "n_output_tokens": tokens[2]},
    }
    (d / "result.json").write_text(json.dumps(result))


def test_outcomes_keep_setup_failures_and_missing_tokens_visible(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial(job, "a__1", "noeta", "a", reward=1.0)
    trial(job, "a__2", "claude-code", "a", reward=0.0, error="AgentTimeoutError")
    trial(job, "b__1", "noeta", "b", reward=None, error="AgentSetupTimeoutError", tokens=None)
    trial(job, "b__2", "claude-code", "b", reward=None, error="EnvironmentStartTimeoutError")
    rows = {r["trial"]: r for r in compare.trial_rows(job, MODEL)}
    assert rows["a__1"]["outcome"] == "pass"
    assert rows["a__2"]["outcome"] == "fail"  # a timed-out trial is graded
    assert rows["b__1"]["outcome"] == "setup-failed"
    assert rows["b__2"]["outcome"] == "infra"
    assert not rows["b__1"]["tokens_reported"]
    text = compare.summarize([job], MODEL)
    noeta = next(line for line in text.splitlines() if line.startswith("| noeta |"))
    cells = [c.strip() for c in noeta.strip("|").split("|")]
    # trials, passed, pass rate, setup failed, infra, rate w/o infra, no tokens
    assert cells[1:8] == ["2", "1", "50.0%", "1", "0", "50.0%", "1"]
    assert (job / "summary.json").is_file()


def test_partial_reward_is_not_a_pass(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial(job, "a__1", "noeta", "a", reward=0.5)
    assert compare.trial_rows(job, MODEL)[0]["outcome"] == "fail"


def test_cost_uses_the_list_price_and_peak_share() -> None:
    off = compare.cost(1_000_000, 900_000, 10_000, MODEL, 0.0)
    assert off == pytest.approx((100_000 * 0.15 + 900_000 * 0.003 + 10_000 * 0.60) / 1e6)
    assert compare.cost(1_000_000, 900_000, 10_000, MODEL, 0.5) == pytest.approx(off * 1.5)
    assert compare.cost(1, 0, 1, "some/other-model", 0.0) is None


def test_peak_fraction_counts_weekday_peak_minutes_only() -> None:
    utc = dt.timezone.utc  # noqa: UP017 - mirrors compare.py
    friday = dt.datetime(2026, 10, 30, 0, 0, tzinfo=utc)
    assert compare.peak_fraction(friday, friday + dt.timedelta(hours=2)) == pytest.approx(0.5)  # 01-02 peak
    saturday = dt.datetime(2026, 10, 31, 1, 0, tzinfo=utc)
    assert compare.peak_fraction(saturday, saturday + dt.timedelta(hours=1)) == 0.0


def test_paired_difference_over_shared_tasks(tmp_path: Path) -> None:
    job = tmp_path / "job"
    for i, (x, y) in enumerate([(1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]):
        trial(job, f"t{i}__n", "noeta", f"t{i}", reward=x)
        trial(job, f"t{i}__c", "claude-code", f"t{i}", reward=y)
    trial(job, "only__n", "noeta", "only", reward=1.0)
    mean, se, n = compare.paired_difference(compare.trial_rows(job, MODEL), "noeta", "claude-code")
    assert n == 3
    assert mean == pytest.approx(1 / 3)
    assert se == pytest.approx(1 / 3)  # diffs 1, 0, 0: sample variance 1/3, over 3 tasks


def test_mirror_builds_prebuilt_images_from_a_from_line(tmp_path: Path) -> None:
    src = tmp_path / "src" / "t"
    (src / "environment").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "environment" / "Dockerfile").write_text("FROM python:3.11\nCOPY . /app\n")
    (src / "tests" / "Dockerfile").write_text("FROM python:3.11\n")
    (src / "task.toml").write_text(
        '[environment]\ncpus = 2\ndocker_image = "org/env:tag@sha256:aa"\n'
        '[verifier]\ntimeout_sec = 60.0\n[verifier.environment]\ndocker_image = "org/ver:tag@sha256:bb"\n')
    images = compare.mirror_dataset(tmp_path / "src", tmp_path / "dst", ["t"])
    dst = tmp_path / "dst" / "t"
    assert images == {"t": ["org/env:tag@sha256:aa", "org/ver:tag@sha256:bb"]}
    assert (dst / "environment" / "Dockerfile").read_text() == "FROM org/env:tag@sha256:aa\n"
    assert (dst / "tests" / "Dockerfile").read_text() == "FROM org/ver:tag@sha256:bb\n"
    assert "docker_image" not in (dst / "task.toml").read_text()
    assert "cpus = 2" in (dst / "task.toml").read_text()


def test_mirror_copies_a_task_harbor_builds_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src" / "t"
    (src / "environment").mkdir(parents=True)
    (src / "environment" / "Dockerfile").write_text("FROM base:1\nRUN true\n")
    (src / "task.toml").write_text("[environment]\ncpus = 1\n")
    assert compare.mirror_dataset(tmp_path / "src", tmp_path / "dst", ["t"]) == {"t": []}
    assert (tmp_path / "dst" / "t" / "environment" / "Dockerfile").read_text() == "FROM base:1\nRUN true\n"


def test_selected_tasks_excludes_and_checks_names(tmp_path: Path) -> None:
    for name in ("a", "b", "gpu"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "task.toml").write_text("")
    assert compare.selected_tasks(tmp_path, None, ["gpu"]) == ["a", "b"]
    assert compare.selected_tasks(tmp_path, ["org/b"], ["gpu"]) == ["b"]
    with pytest.raises(SystemExit):
        compare.selected_tasks(tmp_path, ["missing"], [])


def test_redaction_replaces_the_key_everywhere(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "log.txt").write_text("NOETA_API_KEY=plat_secret_123456 ok")
    (tmp_path / "clean.txt").write_text("nothing here")
    assert compare.redact_tree(tmp_path, ["plat_secret_123456", ""]) == 1
    assert (tmp_path / "a" / "b" / "log.txt").read_text() == "NOETA_API_KEY=<redacted> ok"


def test_leftovers_only_touch_finished_trials_of_this_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = tmp_path / "job"
    trial(job, "Task__AbC1234", "noeta", "task", reward=1.0)
    (job / "Task__Run9999").mkdir()  # still running: no result yet
    calls: list[list[str]] = []
    images = ("sha256:1\ttask__abc1234__env-main:latest\n"
              "sha256:2\ttask__run9999__env-main:latest\n"
              "sha256:3\tsomeone/else:1\n"
              "sha256:4\t<none>:<none>\n"
              "sha256:5\t<none>:<none>\n")
    labels = {"sha256:4": "task__abc1234__verifier__x", "sha256:5": "another__project"}

    def run(cmd: list[str], **_: Any) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:2] == ["docker", "images"]:
            return SimpleNamespace(stdout=images, returncode=0)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout=labels.get(cmd[-1], ""), returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    leftovers = compare.TrialLeftovers(job)
    leftovers.volumes = {"vol-a": "task__abc1234", "vol-b": "task__run9999"}
    leftovers.clean()
    removed = [c[-1] for c in calls if c[:2] in (["docker", "rmi"], ["docker", "volume"])]
    assert removed == ["task__abc1234__env-main:latest", "sha256:4", "vol-a"]
    leftovers.clean(everything=True)
    removed = [c[-1] for c in calls if c[:2] in (["docker", "rmi"], ["docker", "volume"])]
    assert "task__run9999__env-main:latest" in removed and "vol-b" in removed
    assert "sha256:3" not in removed and "someone/else:1" not in removed and "sha256:5" not in removed


def test_a_builder_that_cannot_be_created_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(stdout="", stderr="no buildx", returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    builder = compare.RunBuilder("bench-x")
    with pytest.raises(SystemExit):  # never fall back to the host's default builder
        builder.create()
    builder.prune()
    builder.remove()
    # only the builder this run tried to create is cleaned up; nothing else is pruned
    assert calls[1:] == [["docker", "buildx", "rm", "bench-x"]]
