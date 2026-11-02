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


def agent(*extra: Any, model: str = "m", **kw: Any) -> Agent:
    return Agent(name="a", model=model, extensions=[FileTools(), Skills(**kw), *extra])


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
    skill(ws(h), "deep", "description: Think hard.\nmodel: sonnet\neffort: high\n")
    a = agent(model="claude-opus-5-5")
    h.provider.add(reply("", ("Skill", {"skill": "deep"})), reply("", ("Read", {
        "file_path": str(h.work / "missing")})), reply("done"), reply("next"))
    tid = await h.start(a)
    first, second, third = h.provider.requests[:3]
    assert (first.model, first.effort) == ("claude-opus-5-5", None)
    assert (second.model, second.effort) == ("claude-sonnet-5-5", "high")
    assert (third.model, third.effort) == ("claude-sonnet-5-5", "high")
    await h.say(a, tid, "again")
    assert (h.provider.requests[-1].model, h.provider.requests[-1].effort) \
        == ("claude-opus-5-5", None)


async def test_a_skills_model_ends_with_its_turn_whoever_opens_the_next(h: Harness) -> None:
    """A notice (a background agent's report, a timer) that opens the next
    turn gets the task's model and effort, not the skill's."""
    from noeta.core.types import user

    skill(ws(h), "deep", "description: Think hard.\nmodel: sonnet\neffort: high\n")
    a = agent(model="claude-opus-5-5")
    h.provider.add(reply("", ("Skill", {"skill": "deep"})), reply("done"), reply("seen"))
    tid = await h.start(a)
    assert (h.provider.requests[1].model, h.provider.requests[1].effort) \
        == ("claude-sonnet-5-5", "high")
    await h.client.send(tid, user("a background job ended", origin="system"))
    await h.runtime(a).run_until_idle()
    assert len(h.provider.requests) == 3
    assert (h.provider.requests[-1].model, h.provider.requests[-1].effort) \
        == ("claude-opus-5-5", None)


async def test_a_skills_model_of_another_provider_is_ignored(h: Harness) -> None:
    """A Claude alias in a skill run by a task on another provider's model
    (or an unknown one) leaves the task's model: the request would fail."""
    from noeta.core import models

    skill(ws(h), "deep", "description: Think hard.\nmodel: sonnet\neffort: high\n")
    skill(ws(h), "odd", "description: Odd.\nmodel: no-such-model\n")
    a = agent(model="gpt-5.5-2026-04-24")
    assert models.model_family("gpt-5.5-2026-04-24") == "openai"
    h.provider.add(reply("", ("Skill", {"skill": "deep"})),
                   reply("", ("Skill", {"skill": "odd"})), reply("done"))
    await h.start(a)
    assert [(r.model, r.effort) for r in h.provider.requests] == [
        ("gpt-5.5-2026-04-24", None), ("gpt-5.5-2026-04-24", "high"), ("gpt-5.5-2026-04-24", "high")]


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
    h.provider.add(reply("summary"))
    await h.say(a, tid, command_text(""))
    h.provider.add(reply("after"))
    await h.say(a, tid, "next")
    texts = [m.text for m in h.provider.requests[-1].messages]
    listings = [t for t in texts if t.startswith(ROSTER_HEADER)]
    # The whole listing again, from the skills there are now.
    assert listings == [f"{ROSTER_HEADER}\n\n- keep: Kept, edited."]
    [invoked] = [t for t in texts if t.startswith("These skills were invoked")]
    assert "Original steps." in invoked and "New steps." not in invoked


async def test_a_listing_the_compaction_keeps_is_not_sent_again(h: Harness) -> None:
    """Claude Code lists after a compaction only what the kept messages'
    listings lack (by name and digest)."""
    skill(ws(h), "first", "description: First.\n")
    a = agent(Compaction())
    h.provider.add(reply("ok"))
    tid = await h.start(a)
    skill(ws(h), "second", "description: Second.\n")
    # Listed with the command, so after the kept reply.
    h.provider.add(reply("summary"))
    await h.say(a, tid, command_text(""))
    h.provider.add(reply("after"), reply("later"))
    await h.say(a, tid, "next")
    texts = [m.text for m in h.provider.requests[-1].messages]
    listings = [t for t in texts if t.startswith(ROSTER_HEADER)]
    assert listings == [f"{ROSTER_HEADER}\n\n- second: Second.",
                        f"{ROSTER_HEADER}\n\n- first: First."]
    await h.say(a, tid, "again")  # nothing is listed twice later
    assert len(await h.records(tid, "skills", "listing")) == 2


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


def test_a_listing_cap_that_is_not_a_whole_number_is_ignored(tmp_path: Path) -> None:
    """``skillListingMaxDescChars`` is a positive whole number (Claude
    Code's): ``0.5`` is ignored, not cut to 0 (which the plugin refuses)."""
    from noeta.cli.config import _skills_settings
    from noeta.cli.settings_files import load

    settings = tmp_path / "settings.json"
    settings.write_text('{"skillListingMaxDescChars": 0.5}')
    args = _skills_settings(load([settings], ["user"]), [])
    assert "max_desc_chars" not in args
    Skills(**{k: v for k, v in args.items() if k != "trusted"})
    settings.write_text('{"skillListingMaxDescChars": 200.0}')
    assert _skills_settings(load([settings], ["user"]), [])["max_desc_chars"] == 200


async def test_a_skill_whose_frontmatter_breaks_loads_without_it(h: Harness) -> None:
    (ws(h) / "broken").mkdir(parents=True)
    (ws(h) / "broken" / "SKILL.md").write_text(
        "---\ndescription: Lost.\nthis line is no key\n---\nUse the body.\n")
    (ws(h) / "open").mkdir()
    (ws(h) / "open" / "SKILL.md").write_text("---\nNo closing line.\n")
    h.provider.add(reply("ok"))
    await h.start(agent())
    assert listing(h) == f"{ROSTER_HEADER}\n\n- broken: Use the body.\n- open: ---"


async def test_a_plugins_commands_are_skills_the_model_can_call(h: Harness) -> None:
    from noeta.plugins.markdown_commands import MarkdownCommands

    commands = [{"name": "tools:review", "path": "/plug/commands/review.md",
                 "text": "---\ndescription: Review it.\n---\nReview $ARGUMENTS.\n"},
                {"name": "tools:quiet", "path": "/plug/commands/quiet.md",
                 "text": "---\ndescription: Quiet.\ndisable-model-invocation: true\n---\nx\n"}]
    typed = [{"name": "tools:review", "body": "Review $ARGUMENTS.\n"}]
    a = agent(MarkdownCommands(typed), plugin_commands=commands)
    h.provider.add(reply("", ("Skill", {"skill": "tools:review", "args": "a.py"})),
                   reply("ok"), reply("typed"))
    tid = await h.start(a)
    assert listing(h) == f"{ROSTER_HEADER}\n\n- tools:review: Review it."
    assert h.provider.requests[1].messages[-1].text == "Review a.py.\n"
    # Typing it stays the markdown_commands plugin's: it is added once.
    await h.say(a, tid, "/tools:review b.py")
    assert sum(m.text == "Review b.py." for m in h.provider.requests[-1].messages) == 1
    assert len(await h.records(tid, "skills", "activated")) == 1


async def test_a_review_skill_finds_scoped_variants_by_the_changed_files(h: Harness) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(h.work)], check=True)
    skill(ws(h), "verify", "description: Verify.\n")
    pkg = h.work / "apps" / "web"
    skill(pkg / ".noeta" / "skills", "verify", "description: Verify the web app.\n")
    (pkg / "main.ts").write_text("export {}\n")
    git = ["git", "-C", str(h.work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    (pkg / "main.ts").write_text("export const changed = 1\n")
    h.provider.add(reply("", ("Skill", {"skill": "verify"})), reply("ok"))
    tid = await h.start(agent())
    [nested] = await h.records(tid, "skills", "nested")
    assert nested.data == {"dir": str(pkg / ".noeta" / "skills")}
    [note] = await h.records(tid, "skills", "variants")
    assert note.message is not None and "`apps/web:verify`" in note.message.text


# ---------------------------------------------------------------- former names


async def test_an_old_name_deny_rule_still_denies_the_skill(h: Harness) -> None:
    """Before skills were named by their directories, ``name: ship-it`` named
    this skill: a ``Skill(ship-it)`` deny rule written then still denies it
    (in any mode), and an allow rule still allows it."""
    skill(ws(h), "deploy", "name: ship-it\ndescription: Ship.\n")
    plans = str(h.work.parent / "plans")
    denied = agent(Permissions(mode="bypassPermissions", plans_dir=plans,
                               deny=["Skill(ship-it)"]))
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("ok"))
    tid = await h.start(denied)
    [result] = await h.results(tid)
    assert result.result.is_error and "Skill(ship-it)" in result.result.text
    assert await h.records(tid, "skills", "activated") == []
    allowed = agent(Permissions(mode="dontAsk", plans_dir=plans, allow=["Skill(ship-it)"],
                                risk={"Skill": "high"}))
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("ok"))
    tid = await h.start(allowed)
    [result] = await h.results(tid)
    assert not result.result.is_error, result.result.text


async def test_an_old_name_allow_rule_does_not_reach_a_name_two_skills_had(
        h: Harness) -> None:
    """Two skills that once shared a frontmatter name: a deny rule naming it
    denies both, an allow rule allows neither."""
    skill(ws(h), "one", "name: dup\ndescription: One.\n")
    skill(ws(h), "two", "name: dup\ndescription: Two.\n")
    plans = str(h.work.parent / "plans")
    h.provider.add(reply("", ("Skill", {"skill": "one"}), ("Skill", {"skill": "two"})),
                   reply("ok"))
    tid = await h.start(agent(Permissions(mode="bypassPermissions", plans_dir=plans,
                                          deny=["Skill(dup)"])))
    assert all(r.result.is_error for r in await h.results(tid))
    h.provider.add(reply("", ("Skill", {"skill": "one"})), reply("ok"))
    tid = await h.start(agent(Permissions(mode="dontAsk", plans_dir=plans,
                                          allow=["Skill(dup)"], risk={"Skill": "high"})))
    [result] = await h.results(tid)
    assert result.result.is_error


async def test_an_old_name_preload_loads_the_skill_and_says_its_name(
        h: Harness, caplog: Any) -> None:
    skill(ws(h), "deploy", "name: ship-it\ndescription: Ship.\n", body="Ship it.\n")
    h.provider.add(reply("ok"))
    with caplog.at_level("WARNING", logger="noeta.plugins.skills.extension"):
        tid = await h.start(agent(preload=["ship-it"]))
    [activated] = await h.records(tid, "skills", "activated")
    assert activated.data["name"] == "deploy"
    assert any("'ship-it'" in r.getMessage() and "'deploy'" in r.getMessage()
               for r in caplog.records)


async def test_a_deeper_skill_file_is_not_a_skill_and_is_warned_about(
        h: Harness, caplog: Any) -> None:
    skill(ws(h) / "group", "inner", "description: Too deep.\n")
    skill(ws(h), "top", "description: Top.\n")
    skill(ws(h) / "top" / "examples", "sample", "description: Part of top.\n")
    h.provider.add(reply("ok"))
    with caplog.at_level("WARNING", logger="noeta.plugins.skills.registry"):
        await h.start(agent())
    assert "inner" not in listing(h) and "- top: Top." in listing(h)
    warned = [r.getMessage() for r in caplog.records if "is not loaded" in r.getMessage()]
    assert len(warned) == 1 and "group/inner/SKILL.md" in warned[0]


# ---------------------------------------------------------------- nested commands


async def test_a_command_file_cannot_pass_for_a_plugins_skill(h: Harness,
                                                             tmp_path: Path) -> None:
    """``.noeta/commands/sp/brain.md`` is ``sp:brain`` (Claude Code's name for
    a command in a subdirectory), but a plugin ``sp``'s ``brain`` skill keeps
    that name, and its rules; another command of that directory loads."""
    plugin = tmp_path / "sp" / "skills"
    skill(plugin, "brain", "description: The plugin's.\n", body="Plugin brain.\n")
    commands = h.work / ".noeta" / "commands" / "sp"
    commands.mkdir(parents=True)
    (commands / "brain.md").write_text("---\ndescription: The repo's.\n---\nRepo brain.\n")
    (commands / "other.md").write_text("---\ndescription: Other.\n---\nOther.\n")
    a = agent(plugin_dirs={str(plugin): "sp"})
    h.provider.add(reply("ok"))
    await h.start(a)
    shown = listing(h)
    assert "- sp:brain: The plugin's." in shown and "The repo's." not in shown
    assert "- sp:other: Other." in shown
    names = {c.name: c.description for c in await a.extensions[1].commands(
        CommandContext(cwd=str(h.work)))}
    assert names["sp:brain"] == "The plugin's." and names["sp:other"] == "Other."


# ---------------------------------------------------------------- the walk up


class _Remote:
    """An exec env on another machine: its own home, its own files."""

    local = False

    def __init__(self, cwd: str, home: str, git: set[str]) -> None:
        self.cwd, self.home, self.git = cwd, home, git
        self.stats: list[str] = []

    async def run(self, argv: list[str], **_kw: Any) -> Any:
        from noeta.core.execenv import RunResult

        assert argv[:2] == ["sh", "-c"] and "HOME" in argv[2]
        return RunResult(exit_code=0, stdout=self.home.encode(), stderr=b"")

    async def stat(self, path: str) -> Any:
        self.stats.append(path)
        return object() if path in self.git else None


async def test_the_walk_up_stops_at_the_envs_home_and_is_kept_per_env() -> None:
    """The project directories are looked for as the exec env sees them:
    up to its repository's root or its home (not this worker's), and two
    sandboxes with the same working directory each look for themselves."""
    ext = Skills()
    plain = _Remote("/home/u/proj/sub", "/home/u", set())
    assert await ext.ancestors(plain) == ("/home/u/proj/sub", "/home/u/proj")
    repo = _Remote("/home/u/proj/sub", "/home/u", {"/home/u/proj/sub/.git"})
    assert await ext.ancestors(repo) == ("/home/u/proj/sub",)
    looked = len(plain.stats)
    assert await ext.ancestors(plain) == ("/home/u/proj/sub", "/home/u/proj")
    assert len(plain.stats) == looked


async def test_a_command_added_to_a_commands_directory_of_any_name_is_found(
        h: Harness, tmp_path: Path) -> None:
    """The user's commands directory need not be called ``commands``: a
    file added to it mid-turn is listed before the next model call."""
    own = h.work / "my-prompts"
    own.mkdir()
    (own / "first.md").write_text("---\ndescription: First.\n---\nOne.\n")
    h.provider.add(reply("", ("Write", {"file_path": str(own / "second.md"),
                                        "content": "---\ndescription: Second.\n---\nTwo.\n"})),
                   reply("ok"))
    tid = await h.start(agent(global_commands_dir=own))
    [written] = await h.results(tid)
    assert not written.result.is_error, written.result.text
    assert "- first: First." in listing(h)
    texts = [m.text for m in h.provider.requests[1].messages]
    assert any("- second: Second." in t for t in texts), texts
