"""Skills: the listing, the ``skill`` tool and its response rules,
activation text, ``/<name>``, scripts, allowed-tools, budget fitting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from noeta.core.agent import Agent
from noeta.core.events import Waiting
from noeta.plugins.fs import FileTools
from noeta.plugins.skills import Skills, UsageRank
from noeta.plugins.skills.allowed_tools import parse_allowed_tools
from noeta.plugins.skills.listing import (
    ADDED_HEADER,
    ROSTER_HEADER,
    fit_menu_to_budget,
    listing_lines,
    short_summary,
)
from noeta.plugins.skills.registry import SkillDescription, SkillRegistry
from noeta.testing import reply
from noeta.toolkit.context import estimate_tokens
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
    assert [m.text for m in first.messages] == ["go", f"{ROSTER_HEADER}\n- deploy: Ship the app."]
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
    assert first.result.text == "unknown skill 'hidden'; available: deploy"
    assert second.result.text == "skill must be a non-empty string"


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
    assert msgs[-1].text == f"{ADDED_HEADER}\n- review: Review code."
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
    assert texts[-2:] == ["next", f"{ROSTER_HEADER}\n- review: Review code."]


async def test_slash_command_activates_even_a_user_only_skill(h: Harness) -> None:
    write_skill(skills_dir(h), "hidden", "User only.", body="Secret steps.\n",
                **{"disable-model-invocation": "true"})
    h.provider.add(reply("ok"))
    tid = await h.start(agent(), "/hidden please")
    msgs = h.provider.requests[0].messages
    assert msgs[0].text == "/hidden please"
    assert msgs[1].text.startswith("Activated skill: hidden") and "Secret steps." in msgs[1].text
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["source"] == "user"


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
    assert msgs[2].text == f"{ROSTER_HEADER}\n- deploy: Workspace deploy.\n- lint: Lint."
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
    assert h.provider.requests[0].messages[-1].text == f"{ROSTER_HEADER}\n- borrowed: Borrowed."


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
    assert listing[1] == "- alpha: Very long description sentence."
    assert listing[2] == "- beta: Very long description sentence."


async def test_usage_rank_counts_model_and_user_activations(h: Harness) -> None:
    write_skill(skills_dir(h), "deploy", "Ship.")
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("done"))
    await h.start(agent())
    ranker = UsageRank(h.store)
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
