"""Skills: the listing, the ``skill`` tool and its response rules,
activation text, ``/<name>``, scripts, allowed-tools, budget fitting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Waiting
from noeta.core.extension import Ask, Extension, Gate, TaskView
from noeta.core.types import ToolUse
from noeta.plugins.fs import FileTools
from noeta.plugins.permissions import Permissions
from noeta.plugins.shell import Shell
from noeta.plugins.skills import Skills, UsageRank
from noeta.plugins.skills.allowed_tools import allow_rule_texts, parse_allowed_tools
from noeta.plugins.skills.inline_commands import DISABLED_MARKER
from noeta.plugins.skills.listing import (
    ADDED_HEADER,
    ROSTER_HEADER,
    fit_menu_to_budget,
    listing_lines,
    short_summary,
)
from noeta.plugins.skills.registry import SkillDescription, SkillRegistry, path_patterns
from noeta.plugins.subtasks import SubagentSpec, Subtasks
from noeta.testing import reply
from noeta.toolkit.tokens import estimate_tokens
from tests.plugins.context.conftest import Harness


def write_skill(root: Path, name: str, description: str, body: str = "Do the thing.\n",
                **extra: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = "".join(f"{k}: {v}\n" for k, v in extra.items())
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{fm}---\n{body}")
    return d


def agent(**kw: Any) -> Agent:
    return Agent(name="a", model="m", extensions=[FileTools(), Skills(**kw)])


def skills_dir(h: Harness) -> Path:
    return h.work / ".noeta" / "skills"


async def test_listing_is_recorded_once_and_offers_the_skill_tool(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship the app.")
    write_skill(skills_dir(h), "hidden", "User only.", **{"disable-model-invocation": "true"})
    a = agent()
    h.provider.add(reply("ok"), reply("again"))
    tid = await h.start(a)
    first = h.provider.requests[0]
    # Held for the first request's system turn, after the prompt.
    assert [m.text for m in first.messages] == ["go", f"{ROSTER_HEADER}\n\n- deploy: Ship the app."]
    assert first.messages[1].role == "system"
    assert "Skill" in [t.name for t in first.tools]
    await h.say(a, tid, "more")
    assert len(await h.records(tid, "skills", "listing")) == 1


async def test_no_skills_means_no_listing_but_the_tool_stays(h: Harness) -> None:
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    assert "Skill" in [t.name for t in h.provider.requests[0].tools]
    assert await h.records(tid, "skills") == []


async def test_activation_records_the_body_after_the_result(h: Harness) -> None:
    d = write_skill(skills_dir(h), "deploy", "Ship $ARGUMENTS now.",
                    body="Step one.\n\nScope: $ARGUMENTS\nStep two.\n")
    h.provider.add(reply("", ("Skill", {"skill": "deploy", "args": "prod eu"})), reply("done"))
    tid = await h.start(agent())
    [result] = await h.results(tid)
    assert result.result.text == ("Skill 'deploy' loaded; its instructions will appear in "
                                  "your context from your next step.")
    msgs = h.provider.requests[1].messages
    body = msgs[-1]
    assert body.text == (f"Activated skill: deploy\n\nShip now.\n\nBase directory for this "
                         f"skill: {d}\n\nStep one.\n\nScope: prod eu\nStep two.\n")
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["source"] == "model" and act.data["args"] == "prod eu"


async def test_a_slash_command_fills_the_skill_from_what_follows_it(h: Harness) -> None:
    write_skill(skills_dir(h), "notes", "Release notes.", body="Notes for $0 since $1.\n")
    write_skill(skills_dir(h), "plain", "No placeholder.", body="Do it.\n")
    a = agent()
    h.provider.add(reply("ok"), reply("ok"))
    tid = await h.start(a, "/notes v0.2 'last week'")
    assert h.provider.requests[0].messages[-2].text.endswith("Notes for v0.2 since last week.\n")
    await h.say(a, tid, "/plain fast please")
    assert h.provider.requests[1].messages[-1].text.endswith(
        "Do it.\n\nARGUMENTS: fast please")


async def test_unknown_and_disabled_skills_are_refused(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    write_skill(skills_dir(h), "hidden", "User only.", **{"disable-model-invocation": "yes"})
    h.provider.add(reply("", ("Skill", {"skill": "hidden"})), reply("", ("Skill", {"skill": ""})),
                   reply("done"))
    tid = await h.start(agent())
    first, second = await h.results(tid)
    assert first.result.is_error
    # A user-only skill is named as such, with the way to start it.
    assert first.result.text.startswith("The skill hidden is marked disable-model-invocation")
    assert "/hidden" in first.result.text
    assert second.result.text == "Not a skill name: ''"


async def test_an_unknown_skill_suggests_a_near_name_not_the_whole_list(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    write_skill(skills_dir(h), "review", "Review.")
    h.provider.add(reply("", ("Skill", {"skill": "deplyo"})), reply("", ("Skill", {"skill": "x"})),
                   reply("done"))
    tid = await h.start(agent())
    first, second = await h.results(tid)
    assert first.result.is_error and second.result.is_error
    assert first.result.text == "There is no skill named deplyo. Did you mean deploy?"
    assert second.result.text == "There is no skill named x."


async def test_calling_an_active_skill_again_follows_it_again(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.", body="Scope: $ARGUMENTS\n")
    h.provider.add(reply("", ("Skill", {"skill": "deploy", "args": "eu"})),
                   reply("", ("Skill", {"skill": "deploy", "args": "eu"})),
                   reply("", ("Skill", {"skill": "deploy", "args": "us"})),
                   reply("done"))
    tid = await h.start(agent())
    same = h.provider.requests[2].messages[-1].text
    assert "deploy" in same and "Arguments: eu" in same and "Scope:" not in same
    changed = h.provider.requests[3].messages[-1].text
    assert changed.startswith("(/deploy called again") and changed.endswith("Scope: us\n")
    acts = await h.records(tid, "skills", "activated")
    assert [a.data["args"] for a in acts] == ["eu", "us"]


async def test_a_leading_slash_and_spaces_around_the_name_are_ignored(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    h.provider.add(reply("", ("Skill", {"skill": " /deploy "})), reply("", ("Skill", {"skill": " "})),
                   reply("done"))
    tid = await h.start(agent())
    first, second = await h.results(tid)
    assert not first.result.is_error
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["name"] == "deploy"
    assert second.result.is_error


async def test_skill_may_share_a_reply_with_other_calls(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    (h.work / "f.txt").write_text("hello\n")
    h.provider.add(reply("", ("Skill", {"skill": "deploy"}), ("Read", {"file_path": "f.txt"})),
                   reply("done"))
    tid = await h.start(agent())
    skill_result, read_result = await h.results(tid)
    assert not skill_result.result.is_error
    assert not read_result.result.is_error and "hello" in read_result.result.text
    assert len(await h.records(tid, "skills", "activated")) == 1


def test_skill_schema() -> None:
    from noeta.plugins.skills.extension import SkillTool
    schema = SkillTool(Skills()).input_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False and schema["required"] == ["skill"]
    assert SkillTool(Skills()).name == "Skill"


async def test_new_skills_are_announced_at_the_next_message(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    a = agent()
    h.provider.add(reply("ok"), reply("again"))
    tid = await h.start(a)
    write_skill(skills_dir(h), "review", "Review code.")
    await h.say(a, tid, "next")
    msgs = h.last_messages()
    assert msgs[-2].text == "next"
    assert msgs[-1].text == f"{ADDED_HEADER}\n\n- review: Review code."
    assert len(await h.records(tid, "skills", "listing")) == 2


async def test_a_skill_installed_into_an_empty_task_is_announced(h: Harness) -> None:
    """Installed mid-turn, announced with the person's next message, with no
    pick-up in between (the runner keeps the task)."""
    from noeta.core.tool import ToolContext, ToolOutput, text_output

    class Install:
        name = "Install"
        description = "install"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = False
        clearable = False

        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            write_skill(skills_dir(h), "review", "Review code.")
            await h.client.send(ctx.task_id, "next", when="now")
            return text_output("installed")

    a = Agent(name="a", model="m", tools=[Install()], extensions=[FileTools(), Skills()])
    h.provider.add(reply("", ("Install", {})), reply("ok"))
    await h.start(a)
    first, second = h.provider.requests
    assert first.tools == second.tools  # the prefix does not move
    texts = [m.text for m in second.messages]
    assert texts[-2:] == ["next", f"{ROSTER_HEADER}\n\n- review: Review code."]


async def test_slash_command_activates_even_a_user_only_skill(h: Harness) -> None:
    write_skill(skills_dir(h), "hidden", "User only.", body="Secret steps.\n",
                **{"disable-model-invocation": "true"})
    h.provider.add(reply("ok"))
    tid = await h.start(agent(), "/hidden please")
    msgs = h.provider.requests[0].messages
    assert msgs[0].text == "/hidden please"
    assert msgs[1].origin == "inline"
    assert msgs[1].text.startswith("Base directory for this skill: ")
    assert msgs[1].text.endswith("Secret steps.\n\nARGUMENTS: please")
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["source"] == "user"


async def test_a_command_in_its_tags_puts_the_body_in_the_persons_turn(h: Harness) -> None:
    from noeta.backends.providers.anthropic import AnthropicProvider
    from noeta.core.commands import expand_with
    from noeta.core.extension import CommandContext

    d = write_skill(skills_dir(h), "notes", "Release notes.", body="Notes for $ARGUMENTS.\n")
    skills = Skills()
    text = await expand_with([skills], CommandContext(cwd=str(h.work)), "/notes v0.2")
    assert text == ("<command-message>notes</command-message>\n<command-name>/notes"
                    "</command-name>\n<command-args>v0.2</command-args>")
    h.provider.add(reply("ok"))
    await h.start(Agent(name="a", model="m", extensions=[FileTools(), skills]), text)
    request = h.provider.requests[0]
    assert request.messages[1].text == f"Base directory for this skill: {d}\n\nNotes for v0.2.\n"
    wire = AnthropicProvider(api_key="k").build_body(request)["messages"]
    # The tags and the body share the person's turn, both plain.
    assert [b["text"] for b in wire[0]["content"]] == [text, request.messages[1].text]


async def test_preload_and_tiers(h: Harness, tmp_path: Path) -> None:
    user_dir = tmp_path / "user-skills"
    write_skill(user_dir, "deploy", "Global deploy.")
    write_skill(user_dir, "lint", "Lint.")
    write_skill(skills_dir(h), "deploy", "Workspace deploy.")
    h.provider.add(reply("ok"))
    tid = await h.start(agent(global_dir=user_dir, preload=["lint"]))
    msgs = h.provider.requests[0].messages
    assert msgs[0].text.startswith("Activated skill: lint")
    assert msgs[1].text == "go"
    assert msgs[2].text == f"{ROSTER_HEADER}\n\n- deploy: Workspace deploy.\n- lint: Lint."
    assert (await h.records(tid, "skills", "activated"))[0].data["source"] == "preload"


async def test_untrusted_workspace_skips_its_skill_dirs(h: Harness, tmp_path: Path) -> None:
    write_skill(skills_dir(h), "deploy", "Workspace.")
    write_skill(h.work / ".agents" / "skills", "agentsy", "Agents tier.")
    write_skill(tmp_path / "borrowed", "borrowed", "Borrowed.")
    h.provider.add(reply("ok"))
    seen: list[str] = []

    def trusted(path: str) -> bool:
        seen.append(path)
        return False

    await h.start(agent(dirs=[tmp_path / "borrowed"], trusted=trusted))
    assert set(seen) == {str(h.work)}
    assert h.provider.requests[0].messages[-1].text == f"{ROSTER_HEADER}\n\n- borrowed: Borrowed."


async def test_run_skill_script_asks_and_runs_only_active_scripts(h: Harness) -> None:
    d = write_skill(skills_dir(h), "tools", "Has a script.")
    (d / "scripts").mkdir()
    (d / "scripts" / "hi.sh").write_text('echo "hi $1"\n')
    a = agent(scripts=True)
    h.provider.add(
        reply("", ("run_skill_script", {"skill": "tools", "relpath": "scripts/hi.sh"})),
        reply("", ("Skill", {"skill": "tools"})),
        reply("", ("run_skill_script", {"skill": "tools", "relpath": "scripts/hi.sh",
                                        "args": ["there"]})),
        reply("ran it"),
    )
    tid = await h.start(a)
    first = (await h.results(tid))[0]
    assert first.result.text == "skill 'tools' is not active; cannot run its scripts"
    waiting = (await h.events(tid))[-1].event
    assert isinstance(waiting, Waiting) and waiting.on == "approval"
    await h.client.answer(tid, waiting.ids[0], "allow")
    await h.runtime(a).run_until_idle()
    last = (await h.results(tid))[-1]
    out = json.loads(last.result.text)
    assert out["exit_code"] == 0 and out["stdout_tail"] == "hi there\n"
    assert out["relpath"] == "scripts/hi.sh" and out["interpreter"] == "bash"


async def test_allowed_tools_enforcement_denies_outside_the_grant(h: Harness) -> None:
    write_skill(skills_dir(h), "readonly", "Read only.", **{"allowed-tools": "[Read, Grep]"})
    (h.work / "f.txt").write_text("x\n")
    h.provider.add(
        reply("", ("Skill", {"skill": "readonly"})),
        reply("", ("Read", {"file_path": "f.txt"})),
        reply("", ("Bash", {"command": "echo hi"})),
        reply("done"),
    )
    tid = await h.start(agent(tool_enforcement="deny"))
    _, read, bash = await h.results(tid)
    assert not read.result.is_error
    assert bash.result.is_error and "not in the allowed-tools grant" in bash.result.text


async def test_rank_resolver_is_asked_once_and_shapes_the_fit(h: Harness) -> None:
    long = "Very long description sentence. " * 40
    for name in ("alpha", "beta"):
        write_skill(skills_dir(h), name, long.strip())
    asked: list[int] = []

    async def rank(_view: object) -> dict[str, float]:
        asked.append(1)
        return {"beta": 5.0}

    h.provider.add(reply("ok"))
    await h.start(agent(rank=rank, budget_tokens=60))
    listing = h.provider.requests[0].messages[-1].text.split("\n")
    assert asked == [1]
    assert listing[2] == "- alpha: Very long description sentence."
    assert listing[3] == "- beta: Very long description sentence."


async def test_usage_rank_counts_model_and_user_activations(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("done"))
    await h.start(agent())
    ranker = UsageRank(Client(h.store))
    assert ranker() == {}
    await ranker.wait()
    assert set(ranker()) == {"deploy"} and ranker()["deploy"] > 0.9


# ---------------------------------------------------------------- pure parts


def _registry(**descs: str) -> SkillRegistry:
    return SkillRegistry({n: SkillDescription(name=n, description=d, body="")
                          for n, d in descs.items()})


def test_cjk_estimate_counts_one_per_character() -> None:
    assert estimate_tokens("部署流程") == 4
    assert estimate_tokens("abcdefgh") == 2


def test_short_summary_takes_the_first_sentence_and_clips() -> None:
    assert short_summary("Short.") == "Short."
    assert short_summary("First sentence here. " + "More text " * 30) == "First sentence here."
    clipped = short_summary("x" * 400)
    assert clipped.endswith("…") and estimate_tokens(clipped) <= 24


def test_over_budget_goes_short_then_name_only() -> None:
    entries = [("a", "Alpha does things. " * 10), ("b", "Beta does things. " * 10)]
    fitted = fit_menu_to_budget(entries, ["a", "b"], budget_tokens=20)
    assert fitted == {"a": "Alpha does things.", "b": "Beta does things."}
    fitted = fit_menu_to_budget(entries, ["b", "a"], budget_tokens=10)
    assert fitted == {"a": "", "b": "Beta does things."}


def test_long_summary_is_capped_with_a_marker() -> None:
    [(_, line)] = listing_lines(_registry(a="word " * 1000), budget_tokens=100_000)
    assert line.endswith("… (truncated)") and estimate_tokens(line) <= 386


def test_allowed_tools_parsing() -> None:
    assert parse_allowed_tools("[Read, Bash(git:*)]") == frozenset({"Read", "Bash"})
    assert parse_allowed_tools("Read, Grep") == frozenset({"Read", "Grep"})
    assert parse_allowed_tools("{Read}") is None


async def test_skill_dirs_are_listed_workspace_first_never_claude_codes(
        h: Harness, tmp_path: Path) -> None:
    write_skill(h.work / ".noeta" / "skills", "release-notes", "Draft release notes.")
    write_skill(h.work / ".claude" / "skills", "cc-skill", "Never listed.")
    write_skill(tmp_path / "noeta-user" / "skills", "a-user-skill", "From the user.")
    h.provider.add(reply("ok"))
    await h.start(agent(dirs=[tmp_path / "noeta-user" / "skills"]))
    assert h.provider.requests[0].messages[-1].text == (
        f"{ROSTER_HEADER}\n\n- release-notes: Draft release notes.\n"
        "- a-user-skill: From the user.")


async def test_when_to_use_follows_the_description_in_the_listing(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship the app.",
                when_to_use="Use when the user says ship it.")
    h.provider.add(reply("ok"))
    await h.start(agent())
    assert h.provider.requests[0].messages[1].text == (
        f"{ROSTER_HEADER}\n\n- deploy: Ship the app. - Use when the user says ship it.")


async def test_named_arguments_fill_a_typed_skill(h: Harness) -> None:
    write_skill(skills_dir(h), "notes", "Release notes.", body="Notes for $tag.\n",
                arguments="[tag]")
    h.provider.add(reply("ok"))
    await h.start(agent(), "/notes v0.3")
    assert h.provider.requests[0].messages[-2].text.endswith("Notes for v0.3.\n")


async def test_directory_variables_name_the_skill_and_the_project(h: Harness) -> None:
    d = write_skill(skills_dir(h), "deploy", "Ship.",
                    body="Run ${NOETA_SKILL_DIR}/go.sh in ${CLAUDE_PROJECT_DIR}; "
                         "also ${CLAUDE_SKILL_DIR}.\n")
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("done"))
    await h.start(agent())
    assert h.provider.requests[1].messages[-1].text.endswith(
        f"Run {d}/go.sh in {h.work}; also {d}.\n")


class _AsksForWrites(Extension):
    """Stands in for a permission layer that asks before every write."""

    name = "asks"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="write?") if call.name == "Write" else None


async def test_allowed_tools_run_without_asking_while_the_skill_is_active(h: Harness) -> None:
    d = write_skill(skills_dir(h), "notes", "Notes.",
                    **{"allowed-tools": "Write(/out/**) Bash(${NOETA_SKILL_DIR}/run.sh)"})
    a = Agent(name="a", model="m", extensions=[FileTools(), Skills(), _AsksForWrites()])
    inside, outside = str(h.work / "out" / "n.md"), str(h.work / "n.md")
    h.provider.add(reply("", ("Skill", {"skill": "notes"})),
                   reply("", ("Write", {"file_path": inside, "content": "x"})),
                   reply("", ("Write", {"file_path": outside, "content": "x"})),
                   reply("done"))
    tid = await h.start(a)
    parked = [r.event for r in await h.events(tid) if isinstance(r.event, Waiting)]
    # Inside the rule the write runs; outside it, it asks.
    assert len(parked) == 1
    assert (h.work / "out" / "n.md").exists() and not (h.work / "n.md").exists()
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["allow_rules"] == ["Write(/out/**)", f"Bash({d}/run.sh)"]


def test_allow_rule_texts() -> None:
    assert allow_rule_texts("Bash(git add:*), Read Edit") == ("Bash(git add:*)", "Read", "Edit")
    assert allow_rule_texts("[Read, 'Bash(npm run test)']") == ("Read", "Bash(npm run test)")
    assert allow_rule_texts("- Read\n- Bash(ls -la)") == ("Read", "Bash(ls -la)")
    assert allow_rule_texts("Read *") == ("*",)


def shell_agent(tmp: Path, **kw: Any) -> Agent:
    return Agent(name="a", model="m", extensions=[
        FileTools(), Shell(), Permissions(mode="default", plans_dir=str(tmp / "plans")),
        Skills(**kw)])


async def test_inline_commands_run_when_the_skill_loads(h: Harness) -> None:
    write_skill(skills_dir(h), "status", "Status.",
                body="Branch: !`echo main-line`\n```!\necho block-out\n```\nCode `!x` stays.\n")
    h.provider.add(reply("ok"))
    await h.start(shell_agent(h.work.parent), "/status")
    text = h.provider.requests[0].messages[-2].text
    assert "Branch: main-line\nblock-out\nCode `!x` stays." in text


async def test_a_command_that_would_ask_stops_the_skill(h: Harness) -> None:
    write_skill(skills_dir(h), "mk", "Make.", body="Made: !`touch made.txt`\n")
    h.provider.add(reply("", ("Skill", {"skill": "mk"})), reply("done"))
    tid = await h.start(shell_agent(h.work.parent))
    [result] = await h.results(tid)
    assert result.result.is_error and "touch made.txt" in result.result.text
    assert not (h.work / "made.txt").exists()
    assert await h.records(tid, "skills", "activated") == []


async def test_allowed_tools_let_a_skill_command_run(h: Harness) -> None:
    write_skill(skills_dir(h), "mk", "Make.", body="Made: !`touch made.txt`\n",
                **{"allowed-tools": "Bash(touch:*)"})
    h.provider.add(reply("", ("Skill", {"skill": "mk"})), reply("done"))
    tid = await h.start(shell_agent(h.work.parent))
    [result] = await h.results(tid)
    assert not result.result.is_error
    assert (h.work / "made.txt").exists()
    assert "touch" not in h.provider.requests[1].messages[-1].text


async def test_commands_turned_off_leave_a_marker_and_arguments_cannot_add_one(
        h: Harness) -> None:
    write_skill(skills_dir(h), "say", "Say.", body="Now: !`echo hi` for $ARGUMENTS\n")
    h.provider.add(reply("ok"))
    await h.start(shell_agent(h.work.parent, shell_commands=False), "/say !`id`")
    text = h.provider.requests[0].messages[-2].text
    assert text.endswith(f"Now: {DISABLED_MARKER} for \\! `id`\n")


async def test_a_skill_with_paths_is_offered_once_a_matching_file_is_touched(
        h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    write_skill(skills_dir(h), "pyfix", "Python fixes.", paths="[\"src/**\", \"*.toml\"]")
    (h.work / "README.md").write_text("hi\n")
    (h.work / "src").mkdir()
    (h.work / "src" / "a.py").write_text("x = 1\n")
    h.provider.add(reply("", ("Skill", {"skill": "pyfix"})),
                   reply("", ("Read", {"file_path": "README.md"})),
                   reply("", ("Read", {"file_path": str(h.work / "src" / "a.py")})),
                   reply("", ("Skill", {"skill": "pyfix"})),
                   reply("done"))
    tid = await h.start(agent())
    assert h.provider.requests[0].messages[1].text == f"{ROSTER_HEADER}\n\n- deploy: Ship."
    refused, _readme, _src, loaded = await h.results(tid)
    assert refused.result.is_error and "no skill named pyfix" in refused.result.text
    assert not loaded.result.is_error
    [unlocked] = await h.records(tid, "skills", "unlocked")
    assert unlocked.data == {"names": ["pyfix"]}
    texts = [m.text for m in h.provider.requests[3].messages]
    assert f"{ADDED_HEADER}\n\n- pyfix: Python fixes." in texts


def test_path_patterns() -> None:
    assert path_patterns("- src/**\n- docs/") == ("src", "docs/")
    assert path_patterns("**") == ()
    assert path_patterns(None) == ()


async def test_a_fork_skill_runs_in_a_sub_agent(h: Harness) -> None:
    write_skill(skills_dir(h), "review", "Review.", body="Review $ARGUMENTS closely.\n",
                context="fork", agent="scout")
    main = Agent(name="a", model="m", extensions=[
        FileTools(), Subtasks([SubagentSpec(name="scout", description="Looks.", agent="w")]),
        Skills()])
    worker = Agent(name="w", model="m", extensions=[])
    h.provider.add(reply("", ("Skill", {"skill": "review", "args": "a.py"})),
                   reply("started"), reply("child report"), reply("thanks"))
    tid = await h.client.create("a", "go")
    await h.runtime(main, worker).run_until_idle()
    first = (await h.results(tid))[0]
    assert not first.result.is_error
    [launched] = await h.records(tid, "subtasks", "launched")
    assert launched.data["type"] == "scout" and launched.data["description"] == (
        "Run the review skill")
    assert any(m.text.endswith("Review a.py closely.\n")
               for r in h.provider.requests for m in r.messages)
    assert await h.records(tid, "skills", "activated") == []


def test_an_untrusted_workspace_is_warned_about_once(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / ".noeta" / "skills").mkdir(parents=True)
    every_agent = [Skills(trusted=lambda _cwd: False) for _ in range(3)]
    with caplog.at_level("WARNING", logger="noeta.plugins.skills.extension"):
        for skills in every_agent:
            skills.tiers(str(tmp_path))
            skills.tiers(str(tmp_path))
    warned = [r.getMessage() for r in caplog.records if "is not trusted" in r.getMessage()]
    assert len(warned) == 1
    # Only the skill directories that exist are named.
    assert str(tmp_path / ".noeta" / "skills") in warned[0]
    assert str(tmp_path / ".agents" / "skills") not in warned[0]


def test_an_untrusted_workspace_without_skill_directories_is_not_warned_about(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="noeta.plugins.skills.extension"):
        Skills(trusted=lambda _cwd: False).tiers(str(tmp_path / "plain"))
    assert not [r for r in caplog.records if "is not trusted" in r.getMessage()]


def test_the_untrusted_warnings_remembered_are_bounded(tmp_path: Path) -> None:
    from noeta.plugins.skills import extension

    skills = Skills(trusted=lambda _cwd: False)
    for n in range(extension.WARNED_KEEP + 50):
        (tmp_path / f"w{n}" / ".noeta" / "skills").mkdir(parents=True)
        skills.tiers(str(tmp_path / f"w{n}"))
    assert len(extension._warned_untrusted) == extension.WARNED_KEEP
