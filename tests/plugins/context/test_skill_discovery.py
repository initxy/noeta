"""Skills as Claude Code finds and loads them: names, descriptions, where
they come from, which wins a name, the frontmatter that changes a turn, the
variables a body fills in, and the listing after a compaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.agent import Agent
from noeta.core.events import Recorded
from noeta.core.extension import CommandContext
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import command_text
from noeta.plugins.fs import FileTools
from noeta.plugins.permissions import Permissions
from noeta.plugins.shell import Shell
from noeta.plugins.skills import Skills
from noeta.plugins.skills.listing import ROSTER_HEADER
from noeta.plugins.skills.registry import body_description
from noeta.plugins.skills.usage import skill_usage
from noeta.plugins.subtasks import SubagentSpec, Subtasks
from noeta.testing import reply
from tests.plugins.context.conftest import Harness


def skill(root: Path, directory: str, front: str, body: str = "Do the thing.\n") -> Path:
    d = root / directory
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{front}---\n{body}" if front else body)
    return d


def agent(*extra: Any, **kw: Any) -> Agent:
    return Agent(name="a", model="m", extensions=[FileTools(), Skills(**kw), *extra])


def ws(h: Harness) -> Path:
    return h.work / ".noeta" / "skills"


def listing(h: Harness, request: int = 0) -> str:
    return next(m.text for m in h.provider.requests[request].messages
                if m.text.startswith(ROSTER_HEADER))


async def test_a_skill_is_named_by_its_directory_and_shows_its_own_name(h: Harness) -> None:
    skill(ws(h), "deploy", "name: Deployer\ndescription: Ship.\n")
    skill(ws(h), "spaced", "name: Has Spaces\ndescription: Not shown beside the name.\n")
    skill(ws(h), "lint", "name: lint\ndescription: Lint.\n")
    skill(ws(h), "Café_v2.1", "description: Unicode is fine.\n")
    skill(ws(h), "bad name", "description: A space cannot name a skill.\n")
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("ok"))
    tid = await h.start(agent())
    assert listing(h) == (f"{ROSTER_HEADER}\n\n- Café_v2.1: Unicode is fine.\n"
                          "- deploy (Deployer): Ship.\n- lint: Lint.\n"
                          "- spaced: Not shown beside the name.")
    [result] = await h.results(tid)
    assert not result.result.is_error


async def test_a_skill_without_a_description_is_described_by_its_first_line(
        h: Harness) -> None:
    skill(ws(h), "notes", "", body="\n# Take notes\n\nThen more.\n")
    skill(ws(h), "plain", "name: plain\n", body="Plain words. More.\n")
    h.provider.add(reply("ok"))
    await h.start(agent())
    assert listing(h) == f"{ROSTER_HEADER}\n\n- notes: Take notes\n- plain: Plain words. More."
    assert body_description("x" * 120) == "x" * 97 + "..."
    assert body_description("\n  \n") is None


async def test_only_a_skill_directory_one_level_down_is_a_skill(h: Harness) -> None:
    outer = skill(ws(h), "outer", "description: Outer.\n")
    skill(outer / "inner", "deep", "description: A file of outer's, not a skill.\n")
    (ws(h) / "SKILL.md").write_text("---\ndescription: Not in a directory.\n---\nx\n")
    h.provider.add(reply("", ("Skill", {"skill": "deep"})), reply("ok"))
    tid = await h.start(agent())
    assert listing(h) == f"{ROSTER_HEADER}\n\n- outer: Outer."
    [refused] = await h.results(tid)
    assert refused.result.is_error


async def test_skills_come_from_every_directory_up_to_the_repository(
        h: Harness, tmp_path: Path) -> None:
    repo = h.work
    (repo / ".git").mkdir()
    sub = repo / "pkg" / "app"
    sub.mkdir(parents=True)
    skill(repo / ".noeta" / "skills", "root-skill", "description: At the root.\n")
    skill(repo / "pkg" / ".agents" / "skills", "pkg-skill", "description: In pkg.\n")
    skill(sub / ".noeta" / "skills", "here", "description: Here.\n")
    skill(repo / "pkg" / ".noeta" / "skills", "here", "description: Shadowed.\n")
    skill(tmp_path / ".noeta" / "skills", "above", "description: Above the repository.\n")
    extra = tmp_path / "extra"
    skill(extra / ".noeta" / "skills", "extra-skill", "description: Added.\n")
    managed = tmp_path / "managed"
    skill(managed / ".noeta" / "skills", "here", "description: The machine's.\n")
    skill(managed / ".noeta" / "skills", "policy", "description: Policy.\n")
    h.env_for = lambda _s: __import__(
        "noeta.backends.execenvs.local", fromlist=["LocalExecEnv"]).LocalExecEnv(str(sub))
    h.provider.add(reply("ok"))
    await h.start(agent(additional_dirs=[extra], managed_dir=managed))
    text = listing(h)
    assert "- root-skill: At the root." in text and "- pkg-skill: In pkg." in text
    assert "- extra-skill: Added." in text and "- policy: Policy." in text
    # The machine's skill wins a name, then the nearest directory's.
    assert "- here: The machine's." in text
    assert "above" not in text


async def test_command_files_are_skills_the_model_can_call(h: Harness, tmp_path: Path) -> None:
    commands = h.work / ".noeta" / "commands"
    (commands / "git").mkdir(parents=True)
    (commands / "review.md").write_text("---\ndescription: Review the diff.\n---\n"
                                        "Review $ARGUMENTS.\n")
    (commands / "git" / "sync.md").write_text("Sync with the remote.\n")
    user = tmp_path / "user-commands"
    user.mkdir()
    (user / "mine.md").write_text("---\ndescription: Mine.\n---\nMine.\n")
    h.provider.add(reply("", ("Skill", {"skill": "review", "args": "a.py"})), reply("ok"))
    tid = await h.start(agent(global_commands_dir=user))
    assert listing(h) == (f"{ROSTER_HEADER}\n\n- git:sync: Sync with the remote.\n"
                          "- review: Review the diff.\n- mine: Mine.")
    [result] = await h.results(tid)
    assert not result.result.is_error
    # A command file has no directory of its own: no "Base directory" line.
    assert h.provider.requests[1].messages[-1].text == "Review a.py.\n"
    names = {c.name for c in await Skills(global_commands_dir=user).commands(
        CommandContext(cwd=str(h.work)))}
    assert {"review", "git:sync", "mine"} <= names


async def test_a_skill_the_user_cannot_type_is_still_the_models(h: Harness) -> None:
    skill(ws(h), "helper", "description: Help.\nuser-invocable: false\n", body="Helped.\n")
    a = agent()
    h.provider.add(reply("ok"), reply("ok"))
    tid = await h.start(a)
    assert "- helper: Help." in listing(h)
    await h.say(a, tid, "/helper now")
    assert await h.records(tid, "skills", "activated") == []
    names = {c.name for c in await Skills().commands(CommandContext(cwd=str(h.work)))}
    assert "helper" not in names


async def test_a_skills_model_and_effort_last_the_rest_of_its_turn(h: Harness) -> None:
    skill(ws(h), "deep", "description: Think hard.\nmodel: other-model\neffort: high\n")
    a = agent()
    h.provider.add(reply("", ("Skill", {"skill": "deep"})), reply("", ("Read", {
        "file_path": str(h.work / "missing")})), reply("done"), reply("next"))
    tid = await h.start(a)
    first, second, third = h.provider.requests[:3]
    assert (first.model, first.effort) == ("m", None)
    assert (second.model, second.effort) == ("other-model", "high")
    assert (third.model, third.effort) == ("other-model", "high")
    await h.say(a, tid, "again")
    assert (h.provider.requests[-1].model, h.provider.requests[-1].effort) == ("m", None)


async def test_a_typed_skills_effort_sets_its_turn(h: Harness) -> None:
    skill(ws(h), "quick", "description: Fast.\neffort: low\n")
    a = agent()
    h.provider.add(reply("ok"), reply("typed"))
    tid = await h.start(a)
    await h.say(a, tid, "/quick")
    assert h.provider.requests[-1].effort == "low"


async def test_a_fork_skills_model_goes_to_its_agent(h: Harness) -> None:
    skill(ws(h), "review", "description: Review.\ncontext: fork\nmodel: haiku\n",
          body="Review it.\n")
    main = Agent(name="a", model="m", extensions=[
        FileTools(), Subtasks([SubagentSpec(name="general-purpose", description="Any.",
                                            agent="w")]), Skills()])
    worker = Agent(name="w", model="m", extensions=[])
    h.provider.add(reply("", ("Skill", {"skill": "review"})), reply("started"),
                   reply("report"), reply("thanks"))
    tid = await h.client.create("a", "go")
    await h.runtime(main, worker).run_until_idle()
    [launched] = await h.records(tid, "subtasks", "launched")
    assert launched.data["type"] == "general-purpose"
    child = next(r for r in h.provider.requests if r.messages[0].text.endswith("Review it.\n"))
    assert child.model != "m"


async def test_the_session_id_and_effort_fill_a_skill(h: Harness) -> None:
    skill(ws(h), "ids", "description: Ids.\n",
          body="Session ${CLAUDE_SESSION_ID} at ${CLAUDE_EFFORT}.\n")
    skill(ws(h), "own", "description: Own.\neffort: max\n", body="At ${CLAUDE_EFFORT}.\n")
    h.provider.add(reply("", ("Skill", {"skill": "ids"}), ("Skill", {"skill": "own"})),
                   reply("ok"))
    tid = await h.client.create("a", "go", config={"effort": "high"})
    await h.runtime(agent()).run_until_idle()
    texts = [m.text for m in h.provider.requests[1].messages]
    assert f"Session {tid} at high.\n" in "\n".join(texts)
    assert "At max.\n" in "\n".join(texts)


async def test_a_plugins_skill_fills_in_the_plugins_variables(h: Harness, tmp_path: Path) -> None:
    plugin = tmp_path / "plug" / "skills"
    skill(plugin, "ship", "description: Ship.\n",
          body="Run ${CLAUDE_PLUGIN_ROOT}/bin/ship to ${user_config.region} "
               "with ${user_config.token}; data in ${CLAUDE_PLUGIN_DATA}.\n")
    variables = {"${CLAUDE_PLUGIN_ROOT}": "/plug", "${CLAUDE_PLUGIN_DATA}": "/data/plug",
                 "${user_config.region}": "eu",
                 "${user_config.token}": "[sensitive option 'token' not available in skill "
                                         "content]"}
    h.provider.add(reply("", ("Skill", {"skill": "tools:ship"})), reply("ok"))
    await h.start(agent(plugin_dirs={str(plugin): "tools"},
                        plugin_vars={str(plugin): variables}))
    assert h.provider.requests[1].messages[-1].text.endswith(
        "Run /plug/bin/ship to eu with [sensitive option 'token' not available in skill "
        "content]; data in /data/plug.\n")


async def test_a_file_a_shell_command_showed_unlocks_no_paths_skill(h: Harness) -> None:
    """Claude Code finds skills only on a ``Read`` / ``Edit`` / ``Write``
    path; a shell read counts for instruction files alone."""
    skill(ws(h), "pyfix", "description: Python fixes.\npaths: src/**\n")
    (h.work / "src").mkdir()
    (h.work / "src" / "a.py").write_text("x = 1\n")
    a = Agent(name="a", model="m", extensions=[
        FileTools(), Shell(), Permissions(mode="bypassPermissions"), Skills()])
    h.provider.add(reply("", ("Bash", {"command": "cat src/a.py"})),
                   reply("", ("Skill", {"skill": "pyfix"})), reply("done"))
    tid = await h.start(a)
    assert await h.records(tid, "fs", "read")
    assert await h.records(tid, "skills", "unlocked") == []
    assert (await h.results(tid))[-1].result.is_error


async def test_a_nested_directorys_noeta_skill_wins_over_its_agents_skill(h: Harness) -> None:
    pkg = h.work / "apps" / "web"
    skill(pkg / ".noeta" / "skills", "build", "description: Noeta's.\n")
    skill(pkg / ".agents" / "skills", "build", "description: Agents'.\n")
    (pkg / "main.ts").write_text("export {}\n")
    h.provider.add(reply("", ("Read", {"file_path": str(pkg / "main.ts")})), reply("ok"))
    tid = await h.start(agent())
    text = "\n".join(m.text for m in h.provider.requests[1].messages)
    assert "- build: Noeta's. (from apps/web/.noeta/skills" in text
    assert "Agents'" not in text and "apps/web:build" not in text
    assert len(await h.records(tid, "skills", "nested")) == 2


async def test_new_skills_are_fitted_to_the_budget_by_themselves_and_ranked(
        h: Harness) -> None:
    long = "Very long description sentence. " * 30
    skill(ws(h), "old", f"description: {long.strip()}\n")
    a = agent(budget_chars=300, rank=lambda _v: {"zeta": 9.0})
    h.provider.add(reply("ok"), reply("ok"))
    tid = await h.start(a)
    for name in ("alpha", "zeta"):
        skill(ws(h), name, f"description: {long.strip()}\n")
    await h.say(a, tid, "more")
    later = (await h.records(tid, "skills", "listing"))[-1]
    assert later.data["names"] == ["alpha", "zeta"]
    assert later.data["lines"] == ["- alpha: Very long description sentence.",
                                   "- zeta: Very long description sentence."]


async def test_after_a_compaction_the_listing_is_rebuilt_and_skills_keep_their_text(
        h: Harness) -> None:
    keep = skill(ws(h), "keep", "description: Kept.\n", body="Original steps.\n")
    gone = skill(ws(h), "gone", "description: Gone soon.\n")
    a = agent(Compaction())
    h.provider.add(reply("", ("Skill", {"skill": "keep"})), reply("ok"))
    tid = await h.start(a)
    (keep / "SKILL.md").write_text("---\ndescription: Kept, edited.\n---\nNew steps.\n")
    (gone / "SKILL.md").unlink()
    gone.rmdir()
    await h.say(a, tid, "more")  # the edited skill is listed again
    h.provider.add(reply("summary"))
    await h.say(a, tid, command_text(""))
    h.provider.add(reply("after"))
    await h.say(a, tid, "next")
    texts = [m.text for m in h.provider.requests[-1].messages]
    listings = [t for t in texts if t.startswith(ROSTER_HEADER)]
    # The whole listing again, from the skills there are now: the removed
    # one is gone (a listing the kept tail holds stays as it was).
    assert listings[-1] == f"{ROSTER_HEADER}\n\n- keep: Kept, edited."
    assert not any("gone" in t for t in listings)
    [invoked] = [t for t in texts if t.startswith("These skills were invoked")]
    assert "Original steps." in invoked and "New steps." not in invoked


def _activation(at: float, name: str, task: str = "t") -> Recorded:
    from noeta.core.events import ExtensionRecorded
    del task
    return Recorded(seq=1, at=at, event=ExtensionRecorded(
        extension="skills", kind="activated", data={"name": name, "source": "model"}))


def test_a_skill_counts_once_a_minute_across_tasks() -> None:
    usage = skill_usage([_activation(100.0, "a", "t1"), _activation(130.0, "a", "t2"),
                         _activation(161.0, "a", "t1"), _activation(150.0, "b")])
    assert (usage["a"].count, usage["a"].last_used_at) == (2, 161.0)
    assert usage["b"].count == 1


def test_the_listing_settings_reach_the_plugin(tmp_path: Path,
                                               monkeypatch: Any) -> None:
    from noeta.cli.config import SKILL_BUDGET_ENV, _skills_settings
    from noeta.cli.settings_files import load

    settings = tmp_path / "settings.json"
    settings.write_text('{"skillListingBudgetFraction": 0.02, "skillListingMaxDescChars": 99}')
    monkeypatch.setenv(SKILL_BUDGET_ENV, "5000")
    args = _skills_settings(load([settings], ["user"]), ["/extra"])
    assert args["budget_fraction"] == 0.02 and args["max_desc_chars"] == 99
    assert args["budget_chars"] == 5000 and args["additional_dirs"] == ["/extra"]
    ext = Skills(**{k: v for k, v in args.items() if k != "trusted"})
    assert ext.max_desc_chars == 99 and ext.budget_fraction == 0.02
