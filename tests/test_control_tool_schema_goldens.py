"""Byte-order goldens for the control-tool schema surface.

These literals pin the assembled ``control_action_schemas`` list — the control
tool schemas the composer appends after the executable tools in
``View.provider_tool_schemas`` and folds into the stable-prefix hash — across a
config matrix. It fixes the schema render order (``spawn_subagent``
-> ``todo_write`` -> ``ask_user_question`` -> ``skill`` -> ``run_workflow`` ->
``structured_output``) and the byte content of every schema.

The expected literals are hand-recorded, never derived from the code under
test, so the golden can never follow the regression it exists to catch —
matching the discipline of ``tests/test_session_pack_goldens.py``. Do not
regenerate them without deliberate review: the stable-prefix prompt cache only
hits when this byte order holds.

Construction is implementation-agnostic — sessions are built through the same
public ``build_code_replay_inputs`` seam ``test_session_pack_goldens.py`` uses,
never by importing ``_build_control_action_schemas`` or the ``*_tool_schema``
functions; the observable is the composer's stored control-schema list, the same
altitude that suite reads ``_content_renderers`` at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.presets import official_specs
from noeta.protocols.canonical import to_canonical_bytes
from noeta.storage.memory import InMemoryContentStore

from tests._session_inputs import build_code_replay_inputs


# ---------------------------------------------------------------------------
# Per-tool literal canonical schemas. Each is the exact
# ``to_canonical_bytes(schema).decode("utf-8")`` of that control tool's
# provider-facing dict, recorded pre-migration. Raw strings so the ``\uXXXX`` /
# ``\n`` escapes in the (ASCII-safe) canonical JSON stay literal. A case's
# expected list is these literals joined in schema-render order (:func:`_list`) —
# byte-identical to how canonical list serialization joins its elements.
# ---------------------------------------------------------------------------

_SCHEMA_SPAWN_SUBAGENT = r'''{"function":{"description":"Launch a sub-agent to handle a focused, self-contained task and get its result back.\n\n## What it does\n\nSpawns one sub-agent per call: pick a `subagent_type` from the roster, give it the full task as `prompt`, and label it with a short `description` (3\u20135 words, shown to the user). The agent works in its own context and returns its final text as this call's result; that text is NOT shown to the user, so relay what matters.\n\n**Parallelism**: emit SEVERAL Task calls in ONE assistant turn \u2014 they run CONCURRENTLY and their results come back together. Issuing one Task per turn is strictly sequential; when work is independent, always batch the calls into a single turn.\n\n## When to use\n\n- The task fits a sub-agent type: a read-only scout for broad searches, a general-purpose worker for a self-contained coding task, an architect for a plan.\n- Delegating independent work keeps your own context clean, or the answer means sweeping many files and you only need the conclusion, not the file dumps.\n\n## When NOT to use\n\n- A single-fact lookup where you already know the file or symbol \u2014 just look it up yourself.\n- Work that needs your accumulated conversation context \u2014 a sub-agent starts fresh and sees ONLY its `prompt`, so make the prompt self-contained: the goal, the constraints, and what to return.","name":"Task","parameters":{"properties":{"background":{"description":"Run the sub-agent in the background instead of waiting for it. With background=true you immediately get a 'started' acknowledgement and keep working; the sub-agent runs concurrently and its result is delivered to you automatically when it finishes \u2014 you never poll or wait. Use it for independent, longer-running work (research, a broad scan) you want off the critical path. Omit it (the default) to delegate and wait for the result inline. Only valid on a turn with exactly ONE Task call (a parallel batch is always foreground).","type":"boolean"},"description":{"description":"A short (3-5 word) description of the task.","type":"string"},"prompt":{"description":"The task for the agent to perform.","type":"string"},"subagent_type":{"description":"The type of specialized agent to use for this task. Available: explore \u2014 Read-only scout: fans out (glob/grep/read + read-only shell) to investigate the workspace and report facts (never edits).; plan \u2014 Architect: reads the code and returns a concrete ordered implementation plan (read-only \u2014 never writes any file).","enum":["explore","plan"],"type":"string"}},"required":["description","prompt","subagent_type"],"type":"object"}},"type":"function"}'''

_SCHEMA_TODO_WRITE = r'''{"function":{"description":"Use this tool to create and manage a structured task list for the current session.\n\n## When to use\n\n- Complex multi-step work (roughly three or more distinct steps), or the user handed you several tasks at once.\n- Mark a todo `in_progress` BEFORE you start it and `completed` the moment it is fully done \u2014 keep exactly ONE item `in_progress` at a time.\n- Update the list in real time as work lands; you may batch a TodoWrite with your next tool calls in the same turn.\n\n## When NOT to use\n\n- A single, straightforward step, or a purely conversational / informational request \u2014 the bookkeeping overhead is not worth it.\n\n## Shape\n\nA call replaces the entire list \u2014 always send the FULL list, never a delta; omitting an item deletes it. Each item is `{content, status, activeForm}`: `content` in imperative form (\"Run tests\"), `status` one of `pending` / `in_progress` / `completed`, `activeForm` in present-continuous form (\"Running tests\"), shown while the item is in progress.\n\nOnly mark `completed` when the item truly succeeded; if it is blocked, errored, or tests still fail, keep it `in_progress` and add a follow-up item.","name":"TodoWrite","parameters":{"properties":{"todos":{"description":"The full checklist (replace-all). Each item: {content, status, activeForm}.","items":{"properties":{"activeForm":{"description":"Present-continuous form shown while in_progress, e.g. 'Running tests'.","type":"string"},"content":{"description":"Imperative form of the task, e.g. 'Run tests'.","type":"string"},"status":{"enum":["pending","in_progress","completed"],"type":"string"}},"required":["content","status","activeForm"],"type":"object"},"type":"array"}},"required":["todos"],"type":"object"}},"type":"function"}'''

_SCHEMA_ASK_USER_QUESTION = r'''{"function":{"description":"Use this tool only when you are blocked on a decision that is genuinely the user's to make: one you cannot resolve from the request, the code, or sensible defaults.\n\n- Presents 1\u20134 questions at once. Each question has the complete `question` text (ending with a question mark), a very short `header` chip (max 12 chars, e.g. \"Auth method\"), 2\u20134 `options` (each `{label, description}` \u2014 concise label, trade-off in the description), and `multiSelect` (true when several answers may be picked together).\n- Do not add an \"Other\" option \u2014 the user can always type a custom answer; it is provided automatically.\n- If you recommend an option, put it first and append \"(Recommended)\" to its label.\n- The user's selections are returned so you can proceed.\n\nWhen NOT to use: a reasonable default exists (make it, state the assumption, keep working); \"should I proceed?\" / \"is this right?\" check-ins; facts you can verify yourself in the codebase.","name":"AskUserQuestion","parameters":{"properties":{"questions":{"items":{"properties":{"header":{"description":"Very short chip label (max 12 chars), e.g. 'Auth method'.","type":"string"},"multiSelect":{"description":"Allow selecting several options.","type":"boolean"},"options":{"description":"Distinct choices. An 'Other' free-text option is always added automatically.","items":{"properties":{"description":{"type":"string"},"label":{"type":"string"}},"required":["label","description"],"type":"object"},"maxItems":4,"minItems":2,"type":"array"},"question":{"description":"The complete question, ending with a question mark.","type":"string"}},"required":["question","header","options","multiSelect"],"type":"object"},"maxItems":4,"minItems":1,"type":"array"}},"required":["questions"],"type":"object"}},"type":"function"}'''

_SCHEMA_SKILL = r'''{"function":{"description":"Activate a named skill so its instructions load into the current task.\n\n## What it does\n\nA single call activates ONE skill, chosen by the `skill` parameter \u2014 constrained\nto the roster of skills indexed for this workspace. Activation loads that skill's\ninstructions and capabilities via a state patch, the same channel a pre-loop\nactivation uses. There are no other arguments: just the skill name.\n\n## When to use\n\n- The task matches an available skill \u2014 activate it BEFORE producing other output\n  about the task, so its guidance is in force while you work.\n- The user references a skill by name or types `/<name>`.\n\n## When NOT to use\n\n- The skill you want is not in the roster \u2014 never guess or invent a name; pick\n  only from the listed ones.\n- A skill is already active \u2014 do not re-activate it.\n- No listed skill covers the task \u2014 just proceed without one.\n\n## Preconditions\n\n- The `skill_invocation` capability must be enabled AND the workspace must have at\n  least one indexed skill, otherwise the tool is not offered.\n- The `skill` value must be one of the names in the roster shown on the parameter.","name":"skill","parameters":{"properties":{"skill":{"description":"Name of the skill to activate. Available: alpha-skill \u2014 alpha desc; beta-skill \u2014 beta desc","enum":["alpha-skill","beta-skill"],"type":"string"}},"required":["skill"],"type":"object"}},"type":"function"}'''

_SCHEMA_RUN_WORKFLOW = r'''{"function":{"description":"Run a short Python orchestration script that fans work out to sub-agents and returns a result.\n\n## What it does\n\nSubmits a model-authored orchestration script that runs in a deterministic\nsandbox exposing exactly these names:\n\n- `parallel(items, agent=\"general-purpose\")`: spawn a BATCH of sub-agents at\n  once, wait for them all, and return their answers as a list in spawn order.\n  Each item is a goal string, or a `{\"goal\": ..., \"agent\": ...}` dict to pick a\n  specific sub-agent per item. Use this for the fan-out step INSIDE a workflow \u2014\n  when you also need a loop / branch / dependency chain around it.\n- `agent(goal, agent=\"general-purpose\")`: spawn ONE sub-agent, wait for it, and\n  return its final answer (a string). Sequential `agent()` calls run one after\n  another, so chain them ONLY when a later call needs an earlier result; for\n  independent work use `parallel()` instead.\n- `log(message)`: emit a progress note (returns nothing).\n- `args`: the dict supplied via this tool's `args` parameter.\n\nFinish with `return <value>` \u2014 that value becomes the workflow's answer. The\nscript is not a normal tool: it is interpreted as its own sub-task, so it can\nsuspend and resume across many sub-agent spawns and survive a crash.\n\n## When to use\n\n- You need to ORCHESTRATE sub-agents programmatically: loop over a list, branch\n  the next call on a prior result, or chain steps where each one feeds the next.\n- The work is multi-step across agents \u2014 a dependency chain (`agent()` feeding\n  `agent()`), or fan-out batches you then loop over or combine.\n\n## When NOT to use\n\n- For a single one-off delegation \u2014 just use `spawn_subagent` instead; reaching\n  for a whole script to wrap one spawn is overkill.\n- For plain parallelism with no loop / branch / dependency \u2014 batch the goals\n  into one `spawn_subagent` call's `spawns` array instead; they fan out\n  concurrently without a workflow.\n- For work you can do yourself with the file/search/shell tools; the sub-agents\n  you spawn do the actual I/O, so a workflow only pays off when the work is\n  multi-step or branches across agents.\n\n## Preconditions\n\n- The script MUST be deterministic: no time/random/datetime, no imports, no file\n  or network access (the sub-agents you spawn do the actual I/O). Non-deterministic\n  scripts are rejected before any sub-agent runs.\n- Delegation must be enabled for this agent (the workflow spawns real sub-agents);\n  if the host has not opted into workflows this tool is not offered at all.\n\n## Example\n\nA dependency chain \u2014 scout first, then fan the result out and combine. THIS is\nwhat needs a workflow (the fan-out depends on the scout's output):\n\n    modules = agent(\n        'List the modules missing a docstring, one bare name per line.',\n        agent='explore',\n    )\n    parts = [m.strip() for m in modules.splitlines() if m.strip()]\n    docs = parallel(\n        ['Write a one-line docstring for module: ' + m for m in parts],\n        agent='general-purpose',\n    )\n    return '\\n'.join(docs)","name":"run_workflow","parameters":{"properties":{"args":{"description":"Optional arguments exposed to the script as `args`.","type":"object"},"script":{"description":"The orchestration script (Python). Calls parallel()/agent()/log(), reads args, and uses `return` for the final answer.","type":"string"}},"required":["script"],"type":"object"}},"type":"function"}'''

_SCHEMA_STRUCTURED_OUTPUT = r'''{"function":{"description":"Provide your final answer as a structured object matching the required JSON schema. Call this exactly once when you are done.","name":"structured_output","parameters":{"type":"object"}},"type":"function"}'''


def _list(*schemas: str) -> str:
    """The canonical bytes of the ordered control-schema LIST, composed from the
    per-tool literals: ``[elem0,elem1,...]`` with the ``","`` separator the
    canonical encoder uses (``separators=(",", ":")``). Assembling literals in a
    fixed order is not deriving the expected from production code — the schema
    bytes themselves are the hand-recorded literals above."""
    return "[" + ",".join(schemas) + "]"


def _main_spec() -> Any:
    return official_specs()["main"]


def _control_schemas(tmp_path: Path, **kwargs: Any) -> list[dict[str, Any]]:
    """Build a session the implementation-agnostic way and return the composer's
    stored control-action schema list (the tail appended after the executable
    tool schemas in ``provider_tool_schemas``).

    Skills are made deterministic by wiring away the built-in / global skill
    tiers (``builtin_skills_dirs=()`` + an empty ``global_skills_dir``), so a
    ``skill`` schema appears only when this test indexes a workspace-local skill
    — never from ambient packaged skills.
    """
    empty_global = tmp_path / "_empty_global_skills"
    empty_global.mkdir(exist_ok=True)
    inputs = build_code_replay_inputs(
        workspace_dir=tmp_path,
        agent=_main_spec(),
        content_store=InMemoryContentStore(),
        model="claude-sonnet-4-5",
        builtin_skills_dirs=(),
        global_skills_dir=empty_global,
        **kwargs,
    )
    return inputs.composer._control_action_schemas


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [s["function"]["name"] for s in schemas]


def _canonical(schemas: list[dict[str, Any]]) -> str:
    return to_canonical_bytes(schemas).decode("utf-8")


def _index_two_skills(tmp_path: Path) -> None:
    """Index two workspace-local skills with distinct descriptions so the
    ``skill`` schema's enum + roster is populated (mirrors the skill fixtures in
    ``test_session_pack_goldens.py``)."""
    for name, desc in (("alpha-skill", "alpha desc"), ("beta-skill", "beta desc")):
        d = tmp_path / ".noeta" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody\n",
            encoding="utf-8",
        )


# The two delegate children whose distinct descriptions render into the
# ``spawn_subagent`` schema's ``agent`` enum + roster.
_DELEGATES = frozenset({"explore", "plan"})


# (a) bare build, empty workspace — no control feature active → empty tail.
def test_bare_build_has_no_control_schemas(tmp_path: Path) -> None:
    schemas = _control_schemas(tmp_path)
    assert schemas == []
    assert _canonical(schemas) == _list()  # "[]"


# (b) todo_write alone.
def test_todo_write_alone(tmp_path: Path) -> None:
    schemas = _control_schemas(tmp_path, todo_write_enabled=True)
    assert _names(schemas) == ["TodoWrite"]
    assert _canonical(schemas) == _list(_SCHEMA_TODO_WRITE)


# (b) ask_user_question alone.
def test_ask_user_question_alone(tmp_path: Path) -> None:
    schemas = _control_schemas(tmp_path, ask_user_question_enabled=True)
    assert _names(schemas) == ["AskUserQuestion"]
    assert _canonical(schemas) == _list(_SCHEMA_ASK_USER_QUESTION)


# (b) delegation alone — the two child descriptions render into the schema.
def test_delegation_alone(tmp_path: Path) -> None:
    schemas = _control_schemas(
        tmp_path, delegation_enabled=True, allowed_subtask_agents=_DELEGATES
    )
    assert _names(schemas) == ["Task"]
    assert _canonical(schemas) == _list(_SCHEMA_SPAWN_SUBAGENT)
    # The distinct child descriptions are rendered into the roster enum.
    agent_prop = schemas[0]["function"]["parameters"]["properties"][
        "subagent_type"
    ]
    assert agent_prop["enum"] == ["explore", "plan"]
    assert "Read-only scout" in agent_prop["description"]
    assert "Architect" in agent_prop["description"]


# (b) skill_invocation alone — two indexed workspace skills.
def test_skill_alone(tmp_path: Path) -> None:
    _index_two_skills(tmp_path)
    schemas = _control_schemas(tmp_path)
    assert _names(schemas) == ["skill"]
    assert _canonical(schemas) == _list(_SCHEMA_SKILL)
    skill_prop = schemas[0]["function"]["parameters"]["properties"]["skill"]
    assert skill_prop["enum"] == ["alpha-skill", "beta-skill"]


# (b) workflow alone.
def test_workflow_alone(tmp_path: Path) -> None:
    schemas = _control_schemas(tmp_path, workflow_enabled=True)
    assert _names(schemas) == ["run_workflow"]
    assert _canonical(schemas) == _list(_SCHEMA_RUN_WORKFLOW)


# (c) full combination — exact render ORDER + byte-exact full list.
def test_full_combination_order_and_bytes(tmp_path: Path) -> None:
    _index_two_skills(tmp_path)
    schemas = _control_schemas(
        tmp_path,
        delegation_enabled=True,
        allowed_subtask_agents=_DELEGATES,
        todo_write_enabled=True,
        ask_user_question_enabled=True,
        workflow_enabled=True,
    )
    assert _names(schemas) == [
        "Task",
        "TodoWrite",
        "AskUserQuestion",
        "skill",
        "run_workflow",
    ]
    assert _canonical(schemas) == _list(
        _SCHEMA_SPAWN_SUBAGENT,
        _SCHEMA_TODO_WRITE,
        _SCHEMA_ASK_USER_QUESTION,
        _SCHEMA_SKILL,
        _SCHEMA_RUN_WORKFLOW,
    )


# (d) structured_output — a per-helper declared schema mounts it LAST.
def test_structured_output_is_last(tmp_path: Path) -> None:
    _index_two_skills(tmp_path)
    schemas = _control_schemas(
        tmp_path,
        delegation_enabled=True,
        allowed_subtask_agents=_DELEGATES,
        todo_write_enabled=True,
        ask_user_question_enabled=True,
        workflow_enabled=True,
        structured_output_schema={"type": "object"},
    )
    assert _names(schemas) == [
        "Task",
        "TodoWrite",
        "AskUserQuestion",
        "skill",
        "run_workflow",
        "structured_output",
    ]
    assert _names(schemas)[-1] == "structured_output"
    assert _canonical(schemas) == _list(
        _SCHEMA_SPAWN_SUBAGENT,
        _SCHEMA_TODO_WRITE,
        _SCHEMA_ASK_USER_QUESTION,
        _SCHEMA_SKILL,
        _SCHEMA_RUN_WORKFLOW,
        _SCHEMA_STRUCTURED_OUTPUT,
    )
