"""``TaskCreate`` / ``TaskGet`` / ``TaskUpdate`` / ``TaskList``: Claude Code
2.1.312's task tools, with its schemas, input repairs and result texts. Each
call reads the folded list (``ctx.state``) and records what it changed; the
record lands with the call's result, so a crash leaves both or neither."""

from __future__ import annotations

from importlib import resources
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.tool_input import check_input, tool_use_error
from noeta.plugins.todos.state import STATUSES, TodoState
from noeta.toolkit.tools import SCHEMA_DIALECT

__all__ = ["CREATE", "GET", "LIST", "UPDATE", "TaskCreateTool", "TaskGetTool",
           "TaskListTool", "TaskUpdateTool", "repair_create", "repair_update"]

NAME = "todos"
CREATE, GET, UPDATE, LIST = "TaskCreate", "TaskGet", "TaskUpdate", "TaskList"
#: ``TaskUpdate``'s status that removes the task.
DELETED = "deleted"


def _description(name: str) -> str:
    """The description file as Claude Code sends it: trailing newline kept."""
    return resources.files("noeta.plugins.todos").joinpath(
        "descriptions", f"{name}.md").read_text(encoding="utf-8")


def _todos(ctx: ToolContext) -> TodoState:
    st = ctx.state.get(NAME)
    return st if isinstance(st, TodoState) else TodoState()


def _text(text: str) -> ToolOutput:
    return text_output(text)


_METADATA = {"type": "object", "propertyNames": {"type": "string"}, "additionalProperties": {}}
_ACTIVE_FORM = ('Present continuous form shown in spinner when in_progress '
                '(e.g., "Running tests")')


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"$schema": SCHEMA_DIALECT, "type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


# --------------------------------------------------------------- input repair
# Claude Code repairs a few common wrong shapes before checking the input
# (its ``coerceInput``), and puts a hint ahead of the error for the shapes it
# cannot repair (``validationErrorSteer``).

_CREATE_FIELDS = frozenset({"subject", "description", "activeForm", "metadata"})
_CREATE_ALIASES = ((("title", "name"), "subject"), (("content",), "description"),
                   (("active_form",), "activeForm"))
_UPDATE_ALIASES = ((("id", "task_id"), "taskId"), (("active_form",), "activeForm"))

LIST_STEER = ("TaskCreate creates ONE task per call and has no `tasks` or `todos` parameter. "
              "Call TaskCreate once per task, passing `subject` (a brief title) and "
              "`description` (what needs to be done) as top-level string parameters.")
AGENT_STEER = ("This call used Agent-tool parameters (`prompt`/`subagent_type`). TaskCreate "
               "adds an item to the task list and takes `subject` and `description` string "
               "parameters. To delegate work to a subagent, use the Agent tool instead.")


def _filled(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_list(value: dict[str, Any]) -> bool:
    return "tasks" in value or "todos" in value


def _is_agent(value: dict[str, Any]) -> bool:
    return "prompt" in value or "subagent_type" in value


def _subject_from(description: str) -> str:
    """A subject from a description: its first line, cut at a space near 80
    characters."""
    line = description.split("\n", 1)[0].strip()
    if len(line) <= 80:
        return line
    cut = line[:80]
    space = cut.rfind(" ")
    return (cut[:space] if space > 40 else cut).strip()


def _aliases(value: dict[str, Any],
             aliases: tuple[tuple[tuple[str, ...], str], ...]) -> bool:
    changed = False
    for names, target in aliases:
        for name in names:
            if name in value and target not in value and _filled(value[name]):
                value[target] = value.pop(name)
                changed = True
    return changed


def repair_create(raw: Any) -> dict[str, Any] | None:
    """``TaskCreate``'s input repaired as Claude Code repairs it, or ``None``
    when nothing needed (or could get) repairing."""
    if not isinstance(raw, dict) or _is_list(raw):
        return None
    value = dict(raw)
    if _is_agent(value) and not (_filled(value.get("subject"))
                                 and _filled(value.get("description"))):
        return None
    changed = False
    if "subject" not in value and "description" not in value and "task" in value:
        inner = value["task"]
        if _filled(inner):
            del value["task"]
            value["description"] = inner
        elif isinstance(inner, dict):
            if _is_list(inner) or (_is_agent(inner) and not (
                    _filled(inner.get("subject")) and _filled(inner.get("description")))):
                return None
            del value["task"]
            value.update(inner)
        else:
            return None
        changed = True
    changed |= _aliases(value, _CREATE_ALIASES)
    if _filled(value.get("subject")) and "description" not in value:
        value["description"] = value["subject"]
        changed = True
    elif _filled(value.get("description")) and "subject" not in value:
        value["subject"] = _subject_from(value["description"])
        changed = True
    if _filled(value.get("subject")) and _filled(value.get("description")):
        for key in [k for k in value if k not in _CREATE_FIELDS]:
            del value[key]
            changed = True
        if "activeForm" in value and not isinstance(value["activeForm"], str):
            del value["activeForm"]
            changed = True
        if "metadata" in value and not isinstance(value["metadata"], dict):
            del value["metadata"]
            changed = True
    return value if changed else None


def repair_update(raw: Any) -> dict[str, Any] | None:
    """``TaskUpdate``'s input with Claude Code's renamed keys, or ``None``."""
    if not isinstance(raw, dict):
        return None
    value = dict(raw)
    return value if _aliases(value, _UPDATE_ALIASES) else None


def create_steer(raw: Any) -> str | None:
    """The hint Claude Code puts ahead of a ``TaskCreate`` input error."""
    if not isinstance(raw, dict):
        return None
    inner = raw.get("task") if isinstance(raw.get("task"), dict) else None
    if _is_list(raw) or (inner is not None and _is_list(inner)):
        return LIST_STEER
    if (_is_agent(raw) or (inner is not None and _is_agent(inner))) \
            and not (_filled(raw.get("subject")) and _filled(raw.get("description"))):
        return AGENT_STEER
    return None


class _TaskTool:
    """What the four tools share: their input is repaired and then checked
    here (``unchecked_input``), as Claude Code repairs before it checks."""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only = False
    safe_while_planning = True
    clearable = False
    deferred = True
    #: A call records only together with its result: running it again
    #: after a crash cut it short changes nothing twice.
    retry_safe = True
    unchecked_input = True

    def _repair(self, raw: Any) -> dict[str, Any] | None:
        return None

    def _steer(self, raw: Any) -> str | None:
        return None

    def _checked(self, raw: dict[str, Any]) -> tuple[dict[str, Any], ToolOutput | None]:
        value = self._repair(raw) or raw
        checked, problems = check_input(self.name, self.input_schema, value)
        if problems is None:
            return checked, None
        steer = self._steer(raw)
        message = f"{steer} {problems}" if steer else problems
        return checked, text_output(tool_use_error(message), is_error=True)

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        checked, error = self._checked(input)
        if error is not None:
            return error
        return self._run(checked, ctx)

    def _run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        raise NotImplementedError


class TaskCreateTool(_TaskTool):
    name = CREATE
    description = _description("task_create")
    search_hint = "create a task in the task list"
    input_schema = _schema({
        "subject": {"type": "string", "description": "A brief title for the task"},
        "description": {"type": "string", "description": "What needs to be done"},
        "activeForm": {"type": "string", "description": _ACTIVE_FORM},
        "metadata": {**_METADATA, "description": "Arbitrary metadata to attach to the task"},
    }, ["subject", "description"])

    def _repair(self, raw: Any) -> dict[str, Any] | None:
        return repair_create(raw)

    def _steer(self, raw: Any) -> str | None:
        return create_steer(raw)

    def _run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        item_id = _todos(ctx).next_id()
        data: dict[str, Any] = {"id": item_id, "subject": input["subject"],
                                "description": input["description"]}
        if input.get("activeForm") is not None:
            data["active_form"] = input["activeForm"]
        if input.get("metadata") is not None:
            data["metadata"] = input["metadata"]
        ctx.record(NAME, "created", data)
        return _text(f"Task #{item_id} created successfully: {input['subject']}")


class TaskGetTool(_TaskTool):
    name = GET
    description = _description("task_get")
    search_hint = "retrieve a task by ID"
    read_only = True
    input_schema = _schema(
        {"taskId": {"type": "string", "description": "The ID of the task to retrieve"}},
        ["taskId"])

    def _run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        item = _todos(ctx).get(str(input["taskId"]))
        if item is None:
            return _text("Task not found")
        lines = [f"Task #{item.id}: {item.subject}", f"Status: {item.status}",
                 f"Description: {item.description}"]
        if item.blocked_by:
            lines.append("Blocked by: " + ", ".join(f"#{b}" for b in item.blocked_by))
        if item.blocks:
            lines.append("Blocks: " + ", ".join(f"#{b}" for b in item.blocks))
        return _text("\n".join(lines))


def merged_metadata(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """``new`` merged into ``old``; a ``None`` value deletes its key."""
    out = dict(old or {})
    for key, value in new.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


_STATUS_ERROR = ('The parameter `status` is invalid: Invalid option: expected one of '
                 + "|".join(f'"{s}"' for s in (*STATUSES, DELETED)))


class TaskUpdateTool(_TaskTool):
    name = UPDATE
    description = _description("task_update")
    search_hint = "update a task"
    input_schema = _schema({
        "taskId": {"type": "string", "description": "The ID of the task to update"},
        "subject": {"type": "string", "description": "New subject for the task"},
        "description": {"type": "string", "description": "New description for the task"},
        "activeForm": {"type": "string", "description": _ACTIVE_FORM},
        "status": {"anyOf": [{"type": "string", "enum": list(STATUSES)},
                             {"type": "string", "const": DELETED}],
                   "description": "New status for the task"},
        "addBlocks": {"type": "array", "items": {"type": "string"},
                      "description": "Task IDs that this task blocks"},
        "addBlockedBy": {"type": "array", "items": {"type": "string"},
                         "description": "Task IDs that block this task"},
        "owner": {"type": "string", "description": "New owner for the task"},
        "metadata": {**_METADATA, "description": ("Metadata keys to merge into the task. "
                                                  "Set a key to null to delete it.")},
    }, ["taskId"])

    def _repair(self, raw: Any) -> dict[str, Any] | None:
        return repair_update(raw)

    def _checked(self, raw: dict[str, Any]) -> tuple[dict[str, Any], ToolOutput | None]:
        checked, error = super()._checked(raw)
        status = checked.get("status")
        if error is None and status is not None and status not in (*STATUSES, DELETED):
            problem = f"InputValidationError: {self.name} failed due to the following " \
                      f"issue:\n{_STATUS_ERROR}"
            return checked, text_output(tool_use_error(problem), is_error=True)
        return checked, error

    def _run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        todos = _todos(ctx)
        item_id = str(input["taskId"])
        item = todos.get(item_id)
        if item is None:
            return _text("Task not found")
        status = input.get("status")
        if status == DELETED:
            ctx.record(NAME, "deleted", {"id": item_id})
            return _text(f"Updated task #{item_id} deleted")
        updated: list[str] = []
        fields: dict[str, Any] = {}
        for key, attr in (("subject", "subject"), ("description", "description"),
                          ("activeForm", "active_form"), ("owner", "owner")):
            value = input.get(key)
            if value is not None and value != getattr(item, attr):
                fields[attr] = value
                updated.append(key)
        if input.get("metadata") is not None:
            fields["metadata"] = merged_metadata(item.metadata, input["metadata"])
            updated.append("metadata")
        if status is not None and status != item.status:
            fields["status"] = status
            updated.append("status")
        if fields:
            ctx.record(NAME, "updated", {"id": item_id, "fields": fields})
        # A new link is named even when the other task does not exist (and
        # no link is made), as in Claude Code.
        blocks = [b for b in input.get("addBlocks") or () if b not in item.blocks]
        for other in blocks:
            _link(ctx, todos, item_id, other)
        if blocks:
            updated.append("blocks")
        blocked_by = [b for b in input.get("addBlockedBy") or () if b not in item.blocked_by]
        for other in blocked_by:
            _link(ctx, todos, other, item_id)
        if blocked_by:
            updated.append("blockedBy")
        return _text(f"Updated task #{item_id} {', '.join(updated)}")


def _link(ctx: ToolContext, todos: TodoState, blocker: str, blocked: str) -> None:
    """Record that ``blocker`` blocks ``blocked``, when both exist."""
    if todos.get(blocker) is not None and todos.get(blocked) is not None:
        ctx.record(NAME, "linked", {"blocker": blocker, "blocked": blocked})


class TaskListTool(_TaskTool):
    name = LIST
    description = _description("task_list")
    search_hint = "list all tasks"
    read_only = True
    input_schema = _schema({}, [])

    def _run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        todos = _todos(ctx)
        shown = [i for i in todos.items if not (i.metadata or {}).get("_internal")]
        if not shown:
            return _text("No tasks found")
        done = {i.id for i in shown if i.status == "completed"}
        lines = []
        for item in shown:
            owner = f" ({item.owner})" if item.owner else ""
            blockers = [b for b in item.blocked_by if b not in done]
            blocked = f" [blocked by {', '.join(f'#{b}' for b in blockers)}]" \
                if blockers else ""
            lines.append(f"#{item.id} [{item.status}] {item.subject}{owner}{blocked}")
        return _text("\n".join(lines))


def task_tools() -> tuple[_TaskTool, ...]:
    return (TaskCreateTool(), TaskGetTool(), TaskUpdateTool(), TaskListTool())
