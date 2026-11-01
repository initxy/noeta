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


def _agent_log(tmp_path: Path, lines: list[dict[str, Any]], tail: str = "") -> Path:
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    (agent / "claude-code.txt").write_text("\n".join(json.dumps(x) for x in lines) + "\n" + tail)
    return agent


def test_rate_limits_count_retry_events_not_numbers_in_the_stream(tmp_path: Path) -> None:
    agent = _agent_log(tmp_path, [
        {"type": "system", "subtype": "api_retry", "error_status": 429, "attempt": 1},
        {"type": "system", "subtype": "api_retry", "error_status": 529, "error": "server_error", "attempt": 2},
        {"type": "system", "subtype": "api_retry", "error_status": 500, "attempt": 3},
        {"type": "system", "subtype": "thinking_tokens", "text": "HTTP 429 is handled in client.py"},
        {"type": "system", "subtype": "thinking_tokens", "tokens": 4290},
        {"type": "system", "subtype": "status", "text": "port 4429 or 429?"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "returns 429 when rate limited"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "test_429_retry PASSED"}]}},
    ], tail="Error: 429 Too Many Requests\n")
    # the 429 and 529 retries and the stderr line; not the numbers in thinking, text or tool output
    assert compare.api_errors(agent) == 3


def test_an_error_result_counts_only_for_its_error_text_and_not_after_retries(tmp_path: Path) -> None:
    turns = _agent_log(tmp_path / "a", [
        {"type": "result", "is_error": True, "num_turns": 429, "result": "tool failed"},
        {"type": "result", "is_error": True, "result": "configure the rate limiter in settings.py"},
        {"type": "result", "is_error": True, "result": "both rate limiters reject it"},
    ])
    assert compare.api_errors(turns) == 0
    exhausted = _agent_log(tmp_path / "b", [
        {"type": "system", "subtype": "api_retry", "error_status": 429, "attempt": 1},
        {"type": "system", "subtype": "api_retry", "error_status": 429, "attempt": 2},
        {"type": "result", "is_error": True, "result": "API Error: 429 rate limit exceeded"},
    ])
    assert compare.api_errors(exhausted) == 2  # one exhaustion: counted by its retries
    alone = _agent_log(tmp_path / "c", [
        {"type": "result", "is_error": True, "result": "API Error: 529 overloaded"},
    ])
    assert compare.api_errors(alone) == 1
    named = _agent_log(tmp_path / "d", [
        {"type": "result", "is_error": True, "result": "anthropic.RateLimitError: slow down"},
    ])
    assert compare.api_errors(named) == 1


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


def docker_stub(monkeypatch: pytest.MonkeyPatch, ok: set[str]) -> list[list[str]]:
    """subprocess.run stub: a command succeeds when its first three words
    joined by spaces are in `ok`; every call is recorded."""
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(stdout="", stderr="refused", returncode=0 if " ".join(cmd[:3]) in ok else 1)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def removals(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[1:3] in (["buildx", "rm"], ["rm", "-f"], ["buildx", "prune"]) or c[1] == "rmi"]


def test_a_builder_that_cannot_be_created_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = docker_stub(monkeypatch, {"docker image inspect"})
    builder = compare.RunBuilder("bench-x")
    with pytest.raises(SystemExit):  # never fall back to the host's default builder
        builder.create()
    builder.prune()
    builder.remove()
    assert removals(calls) == []  # nothing was made, so nothing is removed


def test_a_taken_name_stops_the_run_and_touches_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = docker_stub(monkeypatch, {"docker buildx inspect", "docker image inspect"})
    with pytest.raises(SystemExit):
        compare.RunBuilder("bench-x").create()
    calls2 = docker_stub(monkeypatch, {"docker container inspect", "docker image inspect"})
    builder = compare.RunBuilder("bench-y", tmp_path / "state")
    with pytest.raises(SystemExit):
        builder.create()
    builder.remove()
    assert removals(calls) == [] and removals(calls2) == []
    assert not (tmp_path / "state").exists()


def test_a_failed_buildkitd_start_rolls_back_only_its_own_state_dir(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = docker_stub(monkeypatch, {"docker image inspect"})  # `docker run -d` fails
    builder = compare.RunBuilder("bench-z", tmp_path / "state")
    with pytest.raises(SystemExit):
        builder.create()
    assert not (tmp_path / "state").exists()  # the dir this run made is gone
    assert [c for c in removals(calls) if c[1:3] == ["rm", "-f"] or c[1:3] == ["buildx", "rm"]] == []


def test_mirror_keeps_every_compose_service_and_builds_their_images(tmp_path: Path) -> None:
    import yaml

    src = tmp_path / "src" / "t"
    (src / "environment").mkdir(parents=True)
    (src / "environment" / "Dockerfile").write_text("FROM python:3.11\n")
    compose = {"services": {
        "main": {"image": "org/env:tag@sha256:aa", "depends_on": {"db": {"condition": "service_healthy"}},
                 "environment": {"DB_HOST": "db", "TOKEN": "${TOKEN}"}},
        "db": {"image": "org/db:1@sha256:bb", "healthcheck": {"test": ["CMD", "true"]}, "pull_policy": "always"},
        "cache": {"image": "redis:7-alpine", "command": ["redis-server"]},
        "built": {"build": {"context": "./built"}, "image": "org/should-not-tag:1"},
    }, "volumes": {"data": {}}}
    (src / "environment" / "docker-compose.yaml").write_text(yaml.safe_dump(compose))
    (src / "task.toml").write_text('[environment]\ndocker_image = "org/env:tag@sha256:aa"\n')
    images = compare.mirror_dataset(tmp_path / "src", tmp_path / "dst", ["t"])["t"]
    env = tmp_path / "dst" / "t" / "environment"
    mirrored = yaml.safe_load((env / "docker-compose.yaml").read_text())
    assert sorted(mirrored["services"]) == sorted(compose["services"])  # no service lost
    assert mirrored["volumes"] == {"data": {}}
    assert all("image" not in svc for svc in mirrored["services"].values())
    assert mirrored["services"]["main"]["depends_on"] == compose["services"]["main"]["depends_on"]
    assert mirrored["services"]["main"]["environment"]["TOKEN"] == "${TOKEN}"
    assert mirrored["services"]["db"]["build"] == {"context": "./.bench-images/db"}
    assert "pull_policy" not in mirrored["services"]["db"]
    assert mirrored["services"]["built"]["build"] == {"context": "./built"}
    assert (env / ".bench-images" / "db" / "Dockerfile").read_text() == "FROM org/db:1@sha256:bb\n"
    assert (env / ".bench-images" / "cache" / "Dockerfile").read_text() == "FROM redis:7-alpine\n"
    assert (env / "Dockerfile").read_text() == "FROM org/env:tag@sha256:aa\n"
    assert sorted(images) == ["org/db:1@sha256:bb", "org/env:tag@sha256:aa", "redis:7-alpine"]


def test_volumes_count_only_when_mounted_in_this_jobs_containers(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial(job, "Task__AbC1234", "noeta", "task", reward=1.0)
    leftovers = compare.TrialLeftovers(job)
    ev = [{"Type": "container", "Action": "create", "Actor": {"ID": "c1", "Attributes": {"name": "task__abc1234__env-db-1"}}},
          {"Type": "container", "Action": "create", "Actor": {"ID": "c2", "Attributes": {"name": "noeta-test-123"}}},
          {"Type": "volume", "Action": "mount", "Actor": {"ID": "v-ours", "Attributes": {"container": "c1"}}},
          {"Type": "volume", "Action": "mount", "Actor": {"ID": "v-theirs", "Attributes": {"container": "c2"}}},
          {"Type": "volume", "Action": "mount", "Actor": {"ID": "v-unknown", "Attributes": {"container": "c9"}}}]
    for e in ev:
        leftovers.event(json.dumps(e))
    leftovers.event("not json")
    assert leftovers.volumes == {"v-ours": "task__abc1234"}


@pytest.mark.parametrize("state_dir, measured", [("/data/buildkit", "/data/buildkit"), (None, "/var/lib/docker")])
def test_the_builder_is_pruned_by_the_disk_that_holds_its_cache(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state_dir: str | None, measured: str) -> None:
    asked: list[str] = []
    prunes: list[int] = []

    stop = compare.threading.Event()

    def free_gb(path: str = "/var/lib/docker") -> float:
        asked.append(path)
        stop.set()  # one round is enough
        return 1.0 if path == measured else 100.0

    monkeypatch.setattr(compare, "free_gb", free_gb)
    monkeypatch.setattr(compare, "redact_tree", lambda *a: None)
    builder = SimpleNamespace(state_dir=state_dir and Path(state_dir), prune=lambda: prunes.append(1))
    leftovers = SimpleNamespace(clean=lambda: None)
    compare.background(stop, builder, leftovers, tmp_path, [], prune_below_gb=6.0, every_s=0.001)
    assert asked == [measured] and prunes == [1]
