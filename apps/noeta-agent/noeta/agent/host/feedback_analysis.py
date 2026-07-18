"""Internal feedback analysis agent (`__feedback_analysis__`).

An investigative background agent fully isolated from the user-facing space
agent (wired in the same way as `__consolidation__`): the owner triggers a run
manually, it is seeded via the jobs worker as an independent root task and
driven on the WorkerLoop; its tool surface consists entirely of host-side
closures (no fs/shell/browser, never enters a container), and its only output
channel is `submit_suggestion` (structured persistence, evidence mandatory).

Per-run context resolves via `ctx.metadata["task_id"]` (AgentService registers
it after seeding, before handing the lease back — the same technique as the
consolidation on_seeded hook): the tools use it to obtain this run's feedback
list / space root, and transcript reads are restricted to the tasks that were
given feedback.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

FEEDBACK_ANALYSIS_AGENT_NAME = "__feedback_analysis__"

#: Character budget for a single tool output (transcript / reference);
#: anything beyond is clipped (head and tail kept).
_TOOL_OUTPUT_MAX_CHARS = 40_000
#: Clip for a single tool output inside a transcript (process evidence lives
#: in the call chain; the full output is not needed).
_STEP_OUTPUT_CLIP = 400

_ANALYSIS_PROMPT = """\
You are the feedback attribution analyst (an internal platform background agent,
not a user-facing conversation). The input is a batch of negative feedback this
space has accumulated (users marked an AI reply as "not right", with tags /
comments / possibly a "correct result" reference attached).
Your task: attribute this batch of feedback **to its root causes**, and produce
actionable improvement suggestions.

Attribution method (in ascending evidence strength; prefer the strongest available):
1. Feedback tags and text: only hint at a problem category, not enough to pin a root cause.
2. The session process (feedback_transcript): read the tool call chain — should
   the knowledge base have been consulted but wasn't? Consulted but the wrong file
   chosen? Is the skill instruction itself ambiguous? Is there a memory entry that
   should have prevented this error (verify with search_memory)?
3. Reference comparison (feedback_reference): compare the AI's output at the time
   against the human-finalized version point by point, and trace every substantive
   difference back through the transcript to the step where it arose — this is the
   hardest evidence.

Root-cause categories (state in the suggestion body which one applies):
missing memory / skill instruction issue / knowledge base content missing or stale /
misconfiguration / insufficient user input / product defect.

Output discipline (the hard line; quality over quantity):
- The only output channel is the submit_suggestion tool; do not write suggestions
  in prose.
- Every suggestion must carry evidence: cite a concrete feedback_id + one sentence
  explaining how that feedback (and which fact in its transcript / reference)
  supports the suggestion. Do **not** submit guesses without supporting evidence.
- When multiple feedback items point at the same root cause, merge them into one
  suggestion (citing all relevant feedback_ids); do not emit fragmented per-item
  suggestions.
- Pick channel by root cause: memory = a behavior correction that can be saved
  directly as a space memory (attach a memory draft ready to save; the body is the
  draft); skill = a specific space skill needs changing (state the skill_name and
  the concrete change, and provide the **full revised SKILL.md** in skill_patch —
  read the original with read_skill first and change only what is necessary; the
  owner will review a diff preview and apply in one click; builtin skills are
  platform-managed, give no skill_patch — describe in words or use report);
  report = things that need a human decision (model change, missing knowledge
  source, product defect, etc.).
- For feedback that cannot be attributed (insufficient information), do not force
  a suggestion; in the final summary, state which feedback lacked evidence and
  what the user should supplement.

Suggested flow: for each feedback item read the transcript first (when a reference
exists, read the reference first and compare), search relevant memories and skills
to verify, then merge, attribute, and submit suggestions. Finish with a one-paragraph
summary: how many feedback items were analyzed, how many suggestions were submitted,
and which feedback lacked evidence.

Report mode: when the goal asks to summarize "into an improvement report", your
task is not attribution but aggregation — organize the suggestions and feedback
given in the goal into a structured markdown report (problem categories → evidence
overview → suggested action items), submit it via the submit_report tool (the only
output channel, submit exactly once), and do not call submit_suggestion.
"""


@dataclass
class FeedbackRunContext:
    """Tool-side context of one run (registered by task_id after seeding).

    kind=analysis: attribution, output via submit_suggestion; kind=report:
    aggregated report, output via submit_report (triggered_by lands in the
    report row's created_by).
    """

    run_id: str
    space_id: str
    #: feedback_id → feedback dict (with task_id / tags / comment / reference_kind)
    feedback: dict[str, dict]
    #: set of tasks whose transcripts may be read (the tasks that received
    #: feedback; anything else is refused)
    allowed_task_ids: set[str] = field(default_factory=set)
    kind: str = "analysis"
    triggered_by: str = ""
    #: whether the report run has produced output (finalize fails the run when
    #: empty, preventing the "done but no report" false success)
    report_created: bool = False


def _clip_middle(text: str, limit: int) -> str:
    """Over budget: keep head and tail (head = goal/opening context, tail =
    latest progress — the highest-value parts for attribution)."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2 - 40):]
    return head + "\n\n…(middle omitted, too long)…\n\n" + tail


def build_analysis_transcript(events: list[Any]) -> str:
    """UIEvent list → attribution transcript with the tool call chain.

    Unlike workflow/transcript.py (used for handoffs, conversation backbone
    only): attribution needs process evidence, so summary lines for
    tool_call / tool_result / memory_op / skill_activated are kept.
    """
    blocks: list[str] = []
    for ev in events:
        etype = getattr(ev, "type", None)
        data = getattr(ev, "data", None) or {}
        if etype == "user_message":
            text = str(data.get("content") or "").strip()
            if text:
                blocks.append(f"[User]\n{text}")
        elif etype == "assistant_text":
            text = str(data.get("text") or "").strip()
            if text:
                blocks.append(f"[Assistant]\n{text}")
        elif etype == "question":
            qs = "; ".join(
                str(q.get("question", ""))
                for q in (data.get("questions") or [])
                if isinstance(q, dict) and q.get("question")
            )
            if qs:
                blocks.append(f"[Assistant question] {qs}")
        elif etype == "tool_call":
            args = data.get("arguments")
            args_str = json.dumps(args, ensure_ascii=False, default=str)
            if len(args_str) > 300:
                args_str = args_str[:300] + "…"
            blocks.append(f"(tool call {data.get('tool_name', '?')}) {args_str}")
        elif etype == "tool_result":
            out = str(data.get("summary") or data.get("output") or "")
            if len(out) > _STEP_OUTPUT_CLIP:
                out = out[:_STEP_OUTPUT_CLIP] + "…"
            status = "ok" if data.get("success") else "failed"
            blocks.append(f"(tool result {status}) {out}")
        elif etype == "memory_op":
            blocks.append(
                f"(memory {data.get('op', '?')}) {data.get('name', '')}"
            )
        elif etype == "skill_activated":
            blocks.append(f"(skill activated) {data.get('skill', '')}")
    if not blocks:
        return ""
    return _clip_middle("\n\n".join(blocks), _TOOL_OUTPUT_MAX_CHARS)


def build_analysis_goal(
    feedback_items: list[dict],
    space_name: str,
    skill_names: list[str],
    persona: str,
    memory_index: list[tuple[str, str, str]],
) -> str:
    """Assemble one run's goal: the feedback list + space status (overview of
    skills / config / memory index).

    Providing the current state is the key to attribution precision — without
    knowing which skills and memories exist, suggestions cannot land on "which
    one to change". Details (full SKILL.md / transcript / reference) are
    fetched on demand via the tools.
    """
    lines: list[str] = [
        f'Root-cause analysis of {len(feedback_items)} negative feedback items for space "{space_name}".',
        "",
    ]
    lines.append("## Feedback to analyze")
    for fb in feedback_items:
        tags = ", ".join(fb.get("tags") or []) or "(no tags)"
        ref = (
            f"has a reference (read it with feedback_reference; origin {fb.get('reference_origin_url') or 'pasted text'})"
            if fb.get("reference_kind") != "none"
            else "no reference"
        )
        comment = (fb.get("comment") or "").strip() or "(no comment provided)"
        lines.append(
            f"- feedback_id={fb['id']} task_id={fb.get('task_id') or '?'}\n"
            f"  Tags: {tags}; {ref}\n"
            f"  User comment: {comment}"
        )
    lines.append("")
    lines.append("## Space status")
    lines.append(
        "Installed skills: " + (", ".join(skill_names) if skill_names else "(none)")
    )
    if persona.strip():
        lines.append(f"Space agent extra prompt (excerpt):\n{persona.strip()[:2000]}")
    if memory_index:
        lines.append("Existing memory index (name · description):")
        for name, desc, _type in memory_index[:50]:
            lines.append(f"- {name} · {desc}")
    else:
        lines.append("Existing memories: none.")
    return "\n".join(lines)


def build_report_goal(
    suggestions: list[dict],
    feedback_map: dict[str, dict],
    space_name: str,
) -> str:
    """Assemble the report-mode run's goal: the full text of the selected
    suggestions + summaries of the feedback their evidence cites.

    A report is aggregation, not re-attribution — the material goes into the
    goal in full; the tools remain only for verifying details (transcript /
    reference looked up on demand).
    """
    lines = [
        f"Summarize the following {len(suggestions)} improvement suggestions "
        f'for space "{space_name}" into an improvement report, '
        "and submit it via submit_report.",
        "",
        "## Suggestions to summarize",
    ]
    cited: set[str] = set()
    for s in suggestions:
        lines.append(f"### [{s['channel']}] {s['title']} (status={s['status']})")
        if s.get("skill_name"):
            lines.append(f"Skill involved: {s['skill_name']}")
        lines.append(s["body"])
        for ev in s.get("evidence") or []:
            fid = ev.get("feedback_id", "")
            cited.add(fid)
            lines.append(f"- Evidence feedback_id={fid}: {ev.get('note', '')}")
        lines.append("")
    lines.append("## Evidence feedback detail")
    for fid in sorted(cited):
        fb = feedback_map.get(fid)
        if fb is None:
            continue
        tags = ", ".join(fb.get("tags") or []) or "(no tags)"
        ref = "has a reference" if fb.get("reference_kind") != "none" else "no reference"
        lines.append(
            f"- feedback_id={fid} task_id={fb.get('task_id') or '?'}"
            f" ({tags}; {ref}): {(fb.get('comment') or '').strip() or '(no comment provided)'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- tool surface

def build_feedback_analysis_agent(
    *,
    resolve_run: Callable[[Any], Optional[FeedbackRunContext]],
    replay_events: Callable[[str], list[Any]],
    read_reference: Callable[[str, str], Optional[str]],
    skill_roots: Callable[[str], list[Path]],
    memory_root: Callable[[str], Path],
    create_suggestion: Callable[..., dict],
    create_report: Callable[..., dict],
):
    """Build the `__feedback_analysis__` AgentDefinition (with the full set of
    custom tool closures).

    Every dependency is injected as a closure (AgentService assembles them in
    _init_client):
    - resolve_run(ctx) → the run context this call belongs to (resolved via
      ctx.metadata task_id);
    - replay_events(task_id) → the task's UIEvent list (service._replay_single);
    - read_reference(space_id, feedback_id) → the reference snapshot text;
    - skill_roots(space_id) → [space skill root, builtin skill root] (in lookup order);
    - memory_root(space_id) → the space memory directory;
    - create_suggestion(...) → FeedbackStore.create_suggestion (analysis-mode outlet);
    - create_report(...) → FeedbackStore.create_report (report-mode outlet).
    """
    from noeta.sdk import AgentDefinition, Capabilities, ToolResult, tool

    def _no_run() -> ToolResult:
        return ToolResult(
            success=False, output={},
            summary="run context missing (this tool is only usable inside a feedback analysis run)",
        )

    @tool(
        name="feedback_transcript",
        version="1",
        risk_level="low",
        description=(
            "Read the session record of a task that received feedback "
            "(conversation body + tool call chain summary), used to trace "
            "which step the problem arose at. task_id comes from the feedback list."
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    def feedback_transcript(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        task_id = str(arguments.get("task_id") or "")
        if task_id not in run.allowed_task_ids:
            return ToolResult(
                success=False, output={},
                summary=f"task {task_id} is not within this analysis run's feedback scope",
            )
        try:
            text = build_analysis_transcript(replay_events(task_id))
        except Exception:  # noqa: BLE001 - a single task's read failure must not sink the run
            logger.exception("feedback_transcript failed: %s", task_id)
            return ToolResult(
                success=False, output={}, summary="failed to read transcript"
            )
        if not text:
            return ToolResult(
                success=False, output={}, summary="this task has no readable conversation record"
            )
        return ToolResult(
            success=True,
            output={"task_id": task_id, "transcript": text},
            summary=f"transcript of {task_id} ({len(text)} chars)",
        )

    @tool(
        name="feedback_reference",
        version="1",
        risk_level="low",
        description=(
            "Read the reference attached to a feedback item (the correct result "
            "provided by the user / a human-finalized snapshot), used for "
            "comparison-based attribution against the AI's output at the time."
        ),
        input_schema={
            "type": "object",
            "properties": {"feedback_id": {"type": "string"}},
            "required": ["feedback_id"],
        },
    )
    def feedback_reference(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        fid = str(arguments.get("feedback_id") or "")
        if fid not in run.feedback:
            return ToolResult(
                success=False, output={},
                summary=f"feedback {fid} is not within this analysis run's scope",
            )
        text = read_reference(run.space_id, fid)
        if not text:
            return ToolResult(
                success=False, output={}, summary="this feedback has no reference"
            )
        return ToolResult(
            success=True,
            output={"feedback_id": fid, "reference": _clip_middle(text, _TOOL_OUTPUT_MAX_CHARS)},
            summary=f"reference of {fid} ({len(text)} chars)",
        )

    @tool(
        name="read_skill",
        version="1",
        risk_level="low",
        description=(
            "Read the full SKILL.md of a skill (space skills first, then "
            "platform builtins), used to judge whether the problem lies in the "
            "skill instructions themselves."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    def read_skill(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        name = str(arguments.get("name") or "").strip()
        # Path-traversal guard: a skill name is a directory name, no separators allowed
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return ToolResult(success=False, output={}, summary="invalid skill name")
        for root in skill_roots(run.space_id):
            path = root / name / "SKILL.md"
            try:
                if path.is_file():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    return ToolResult(
                        success=True,
                        output={
                            "name": name,
                            "scope": "space" if root == skill_roots(run.space_id)[0] else "builtin",
                            "content": _clip_middle(text, _TOOL_OUTPUT_MAX_CHARS),
                        },
                        summary=f"SKILL.md of {name}",
                    )
            except OSError:
                continue
        return ToolResult(
            success=False, output={}, summary=f"skill {name} does not exist"
        )

    @tool(
        name="search_memory",
        version="1",
        risk_level="low",
        description=(
            "Search this space's long-term memory (read-only), used to verify "
            "whether a memory already corrects a given problem, or to check for "
            "duplicates before suggesting a memory write."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    def search_memory(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output={}, summary="query must not be empty")
        from noeta.sdk import MemoryStore

        store = MemoryStore(memory_root(run.space_id))
        hits = store.search(query)
        results = [
            {"name": name, "matches": list(lines)[:5]} for name, lines in hits[:10]
        ]
        return ToolResult(
            success=True,
            output={"query": query, "results": results},
            summary=f"memory search '{query}': {len(results)} hits",
        )

    @tool(
        name="submit_suggestion",
        version="1",
        risk_level="low",
        description=(
            "Submit one structured improvement suggestion (the only output "
            "channel of analysis mode). channel: memory = a behavior correction "
            "that can be saved directly as a space memory (the body is the memory "
            "draft); skill = change a space skill (skill_name required, and where "
            "possible provide the full revised SKILL.md in skill_patch so the "
            "owner can apply it in one click; only space skills may carry a "
            "patch); report = problems that need a human decision. evidence is "
            "required: cite each supporting feedback_id with a one-sentence basis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["memory", "skill", "report"]},
                "title": {"type": "string", "description": "one-sentence suggestion title"},
                "body": {"type": "string", "description": "suggestion body (markdown)"},
                "skill_name": {"type": "string", "description": "required when channel=skill"},
                "skill_patch": {
                    "type": "string",
                    "description": "the full revised SKILL.md (provide when channel=skill and the skill is a space skill)",
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "feedback_id": {"type": "string"},
                            "note": {"type": "string", "description": "how this feedback supports the suggestion"},
                        },
                        "required": ["feedback_id", "note"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["channel", "title", "body", "evidence"],
        },
    )
    def submit_suggestion(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        if run.kind != "analysis":
            return ToolResult(
                success=False, output={},
                summary="report mode uses submit_report; no suggestions are produced",
            )
        channel = str(arguments.get("channel") or "")
        title = str(arguments.get("title") or "")
        body = str(arguments.get("body") or "")
        skill_name = arguments.get("skill_name")
        evidence = arguments.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return ToolResult(
                success=False, output={},
                summary="evidence is required: suggestions without supporting evidence are rejected",
            )
        cleaned: list[dict] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("feedback_id") or "")
            note = str(item.get("note") or "").strip()
            if fid not in run.feedback:
                return ToolResult(
                    success=False, output={},
                    summary=f"evidence cites a feedback_id outside this run's scope: {fid}",
                )
            if not note:
                return ToolResult(
                    success=False, output={},
                    summary="every evidence item must carry a note (how the feedback supports the suggestion)",
                )
            cleaned.append({"feedback_id": fid, "note": note})
        if not cleaned:
            return ToolResult(
                success=False, output={}, summary="evidence format is invalid"
            )
        if channel == "skill" and not (skill_name and str(skill_name).strip()):
            return ToolResult(
                success=False, output={},
                summary="channel=skill requires skill_name",
            )
        skill_patch = arguments.get("skill_patch")
        if skill_patch is not None and not isinstance(skill_patch, str):
            skill_patch = None
        if skill_patch and skill_patch.strip():
            # A patch may only attach to an **existing space skill**: builtin
            # skills are platform-managed and a nonexistent skill has nothing
            # to apply to — reject both, so the agent degrades to a textual
            # suggestion.
            name = str(skill_name or "").strip()
            space_root = skill_roots(run.space_id)[0]
            if not (space_root / name / "SKILL.md").is_file():
                return ToolResult(
                    success=False, output={},
                    summary=(
                        f"{name} is not a space skill (builtin or nonexistent), "
                        "so it cannot carry a skill_patch — drop the patch and "
                        "describe the change in words"
                    ),
                )
        else:
            skill_patch = None
        try:
            suggestion = create_suggestion(
                space_id=run.space_id,
                run_id=run.run_id,
                channel=channel,
                title=title,
                body=body,
                evidence=cleaned,
                skill_name=str(skill_name).strip() if skill_name else None,
                skill_patch=skill_patch,
            )
        except ValueError as e:
            return ToolResult(success=False, output={}, summary=str(e))
        return ToolResult(
            success=True,
            output={"suggestion_id": suggestion["id"]},
            summary=f"suggestion submitted: [{channel}] {title[:40]}",
        )

    @tool(
        name="submit_report",
        version="1",
        risk_level="low",
        description=(
            "Submit the aggregated report (the only output channel of report "
            "mode, call it exactly once). body is the complete markdown report: "
            "problem categories → evidence overview → suggested action items."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "report title"},
                "body": {"type": "string", "description": "report body (markdown)"},
            },
            "required": ["title", "body"],
        },
    )
    def submit_report(arguments, ctx):
        run = resolve_run(ctx)
        if run is None:
            return _no_run()
        if run.kind != "report":
            return ToolResult(
                success=False, output={},
                summary="analysis mode uses submit_suggestion; no report is produced",
            )
        if run.report_created:
            return ToolResult(
                success=False, output={}, summary="this run has already submitted a report"
            )
        try:
            report = create_report(
                space_id=run.space_id,
                run_id=run.run_id,
                title=str(arguments.get("title") or ""),
                body=str(arguments.get("body") or ""),
                created_by=run.triggered_by,
            )
        except ValueError as e:
            return ToolResult(success=False, output={}, summary=str(e))
        run.report_created = True
        return ToolResult(
            success=True,
            output={"report_id": report["id"]},
            summary=f"report submitted: {report['title'][:40]}",
        )

    return AgentDefinition(
        description=(
            "Internal feedback attribution agent: investigates the root causes "
            "of negative feedback (transcript tracing + reference comparison + "
            "memory/skill verification) and produces evidence-backed improvement "
            "suggestions via submit_suggestion."
        ),
        prompt=_ANALYSIS_PROMPT,
        tools=(
            feedback_transcript,
            feedback_reference,
            read_skill,
            search_memory,
            submit_suggestion,
            submit_report,
        ),
        capabilities=Capabilities(),
    )
