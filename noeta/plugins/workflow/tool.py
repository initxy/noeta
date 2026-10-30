"""The ``Workflow`` tool: finds the script (given, saved by name, or a
file), checks it, records the run and starts its first agents."""

from __future__ import annotations

import posixpath
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from noeta.core.extension import TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message
from noeta.plugins.workflow.engine import EngineMissing
from noeta.toolkit.paths import noeta_home
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description, save_spilled, task_spill_dir

if TYPE_CHECKING:
    from noeta.plugins.workflow import Orchestration

TOOL = "Workflow"
RUN_ID = re.compile(r"^wf_[a-z0-9-]{6,}$")
MAX_SCRIPT_CHARS = 524_288
#: Saved workflows in the project; the user's are under ``<home>/workflows``.
PROJECT_WORKFLOWS = ".noeta/workflows"

NO_ENGINE = ("Workflow cannot run scripts on the worker that runs this task: its embedded "
             "JavaScript engine does not load here ({reason}). Nothing was started. Use the "
             "Agent tool instead.")
STARTED = ('Workflow "{name}" started in the background as {run_id}: {launched} agent(s) '
           "running. A <task-notification> arrives when it finishes; do not wait for it or "
           "guess its result.{saved}")
SAVED = (" The script is saved at {path}: to change the workflow, edit that file and call "
         "Workflow with scriptPath (add resumeFromRunId to reuse finished agents).")


class WorkflowTool:
    name = TOOL
    description = load_description("noeta.plugins.workflow", "workflow")
    read_only = False
    clearable = False
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": MAX_SCRIPT_CHARS, "description": (
                "The whole workflow script: it starts with `export const meta = { name, "
                "description, phases }` (a pure literal, nothing computed), then the body "
                "using agent() / parallel() / pipeline() / phase().")},
            "name": {"type": "string", "description": (
                "The name of a saved workflow (in the project's .noeta/workflows/ or "
                "~/.noeta/workflows/ as <name>.js), which supplies the script.")},
            "description": {"type": "string", "description": (
                "Ignored: the description lives in the script's `meta`.")},
            "title": {"type": "string", "description": (
                "Ignored: the title lives in the script's `meta`.")},
            "args": {"description": (
                "Any value; the script reads it as the global `args`, unchanged. Pass lists "
                "and objects as JSON values (`args: [\"a.ts\", \"b.ts\"]`), never as a string "
                "of JSON: a string reaches the script as one string, so `args.map` fails. Use "
                "it to give a saved workflow its input, such as a question or a path.")},
            "scriptPath": {"type": "string", "description": (
                "The path of a script file to run. Every run saves its script and returns the "
                "path; edit that file and pass it here instead of resending the script. Wins "
                "over `script` and `name`.")},
            "resumeFromRunId": {"type": "string", "pattern": "^wf_[a-z0-9-]{6,}$",
                                "description": (
                "An earlier run of this task to resume: agent() calls whose prompt and options "
                "did not change return that run's results, and only edited or new calls run. "
                "The earlier run must have ended.")},
        },
        "additionalProperties": False,
    }

    def __init__(self, ext: Orchestration, view: TaskView) -> None:
        self.ext = ext
        self.view = view

    async def _script(self, input: Mapping[str, Any], ctx: ToolContext) -> str | ToolOutput:
        path = input.get("scriptPath")
        if isinstance(path, str) and path:
            try:
                return (await ctx.env.read_bytes(path)).decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return text_output(f"{TOOL}: cannot read the script {path}: {exc}", is_error=True)
        name = input.get("name")
        if isinstance(name, str) and name:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                return text_output(f"{TOOL}: {name!r} is not a workflow name.", is_error=True)
            cwd = self.view.cwd
            try:
                return (await ctx.env.read_bytes(f"{cwd}/{PROJECT_WORKFLOWS}/{name}.js")
                        ).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                pass
            try:
                return (self.ext.home or noeta_home()).joinpath(
                    "workflows", f"{name}.js").read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                pass
            known = await self._saved_names(ctx)
            return text_output(f"{TOOL}: no saved workflow is named {name!r}. Saved "
                               f"workflows: {', '.join(known) or '(none)'}.", is_error=True)
        script = input.get("script")
        if isinstance(script, str) and script.strip():
            return script
        return text_output(f"{TOOL}: give `script`, `scriptPath` or `name`.", is_error=True)

    async def _saved_names(self, ctx: ToolContext) -> list[str]:
        """The saved workflows' names, the project's then the user's."""
        names: list[str] = []
        project = f"{self.view.cwd}/{PROJECT_WORKFLOWS}"
        try:
            out = await ctx.env.run(["ls", "-1", project], timeout=5, output_cap=1 << 16)
            if out.exit_code == 0:
                names += out.stdout.decode("utf-8", "replace").split("\n")
        except (OSError, FileNotFoundError):
            pass
        try:
            names += [p.name for p in (self.ext.home or noeta_home()).joinpath(
                "workflows").iterdir()]
        except OSError:
            pass
        seen = [n[:-3] for n in names if n.endswith(".js") and len(n) > 3]
        return list(dict.fromkeys(seen))

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        script = await self._script(input, ctx)
        if isinstance(script, ToolOutput):
            return script
        if len(script) > MAX_SCRIPT_CHARS:
            return text_output(f"{TOOL}: the script is over {MAX_SCRIPT_CHARS} characters.",
                               is_error=True)
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]",
                     script):
            return text_output(f"{TOOL}: the script holds control or invisible characters "
                               "that the approval prompt would hide.", is_error=True)
        runs = self.ext.runs(self.view)
        results: dict[str, Any] = {}
        resume = input.get("resumeFromRunId")
        if resume is not None:
            if not isinstance(resume, str) or not RUN_ID.match(resume) or resume not in runs:
                return text_output(f"{TOOL}: no earlier run {resume!r} in this task.",
                                   is_error=True)
            if runs[resume]["status"] == "running":
                return text_output(f"{TOOL}: run {resume} is still running; wait for it to end "
                                   "before resuming from it.", is_error=True)
            results = dict(runs[resume]["results"])
        try:
            checked = await self.ext.engine.check(script)
        except EngineMissing as exc:
            return text_output(NO_ENGINE.format(reason=exc), is_error=True)
        if "error" in checked:
            return text_output(f"{TOOL}: {checked['error']}. The script must be plain "
                               "JavaScript that starts with the meta literal.", is_error=True)
        meta = checked["meta"]
        rid = f"wf_{secrets.token_hex(5)}"
        path = await self._save(ctx, rid, script)
        run: dict[str, Any] = {
            "run_id": rid, "name": str(meta["name"]), "description": str(meta["description"]),
            "phases": meta.get("phases") or [], "script": script, "args": input.get("args"),
            "call_id": ctx.call_id, "started_at": time.time(), "results": results,
            "script_path": path}
        ctx.record(self.ext.name, "run", run)
        run = {**run, "status": "running", "agents": {}, "failed": [], "logs": 0, "spent": 0}
        view = _ToolView(ctx)

        def spawn(agent: str, prompt: str, config: dict[str, Any], key: str) -> str:
            assert ctx.spawn is not None
            return ctx.spawn(agent, prompt, config=config, background=True)

        launched = await self.ext.drive(view, self.view, run, spawn)
        ctx.notify(f'workflow "{run["name"]}" started: {meta["description"]}', kind="workflow",
                   data={"run_id": rid, "name": run["name"], "description": run["description"],
                         "phases": run["phases"]})
        saved = SAVED.format(path=path) if path else ""
        return text_output(STARTED.format(name=run["name"], run_id=rid, launched=launched,
                                          saved=saved), run_id=rid, script_path=path)

    @staticmethod
    async def _save(ctx: ToolContext, rid: str, script: str) -> str:
        """Keep the script beside the task's other spilled files (in its own
        directory there)."""
        if not ctx.spill_dir:
            return ""
        task_dir = task_spill_dir(ctx.spill_dir, ctx.task_id)
        path = posixpath.join(task_dir, "workflows", f"{rid}.js")
        return path if await save_spilled(ctx, path, script.encode("utf-8")) else ""


class _ToolView:
    """What ``Orchestration.drive`` needs while the tool runs: notices and records
    go through the call's context (recorded with its result), the end
    notice through the task's inbox."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.post: Callable[..., Awaitable[None]] | None = self._post if ctx.post else None

    async def _post(self, body: Any, dedupe_key: str | None = None) -> None:
        assert self._ctx.post is not None
        await self._ctx.post(body, dedupe_key=dedupe_key)

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None:
        self._ctx.notify(text, kind=kind, data=data)

    def record(self, extension: str, kind: str, data: Any = None, *,
               message: Message | None = None) -> None:
        self._ctx.record(extension, kind, data)
