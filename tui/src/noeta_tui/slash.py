"""The terminal UI's own slash commands with a mechanism behind them, as
Claude Code has them: ``/help``, ``/status``, ``/model``, ``/effort``, ``/context``,
``/cost``, ``/copy``, ``/memory``, ``/config``, ``/permissions``, ``/agents``,
``/rewind``, ``/clear``, ``/plugins``, ``/reload-plugins``, and ``/mcp`` ``/hooks`` (what
the MCP and hooks plugins say, shown as a panel). Each takes the app and what
was typed after the name; what it shows goes to the transcript, never to
the model. Plugins' commands (``/compact``, skills, ``.noeta/commands``)
run through ``AppAPI.run_command``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text

from noeta.core import models
from noeta.core.events import ExtensionRecorded, ModelReplied, TaskCreated
from noeta.host.app import change_config, model_choices, prepare_model
from noeta.host.rewind import RewindPoint, lineage, rewind_points
from noeta.host.usage import cost_text, tree_usage
from noeta.toolkit.permission_modes import MODE_NAMES
from noeta.toolkit.plugin_records import MEMORY
from noeta_tui import clipboard, plugin_ui, render
from noeta_tui.transcript import SERVER_DIRECTORY
from noeta_tui.widgets import Option, Picker

if TYPE_CHECKING:
    from noeta_tui.app import NoetaApp

__all__ = ["COMMANDS", "matches", "resume_picker", "run", "trust_picker"]

#: name -> what /help and the slash menu say. The app's own: /new, /resume,
#: /stop, /exit.
COMMANDS = {
    "help": "Show help and the commands",
    "status": "Show the version, model, task, directory and settings",
    "model": "Pick or set the model (from the next request on)",
    "effort": "Pick or set how hard the model thinks (low … max)",
    "context": "Show what fills the next request, as a grid",
    "cost": "Show the tokens and cost of this task and its sub-agents",
    "copy": "Copy the last reply to the clipboard (or /copy N for the Nth-latest)",
    "memory": "List the instruction and memory files; open one in $EDITOR",
    "config": "Show the settings; edit a settings file (edit user|project|local)",
    "permissions": "Show the permission mode and rules",
    "agents": "List the agents this front end runs",
    "mcp": "Show the MCP servers: connected, failed or waiting for a sign-in",
    "hooks": "Show the hooks configured (read-only)",
    "rewind": "Go back to before a prompt: conversation, code or both (also Esc Esc)",
    "clear": "Start a new task (also /new)",
    "new": "Start a new task",
    "resume": "Pick a task to resume",
    "stop": "Stop the turn (also Esc)",
    "plugins": "Switch plugins for this task or new tasks; remove or install them",
    "reload-plugins": "Load the plugin, agent and settings files again (from the next turn on)",
    "exit": "Quit",
}
#: Turns the /rewind picker restores.
REWIND_WHAT = [Option("both", "Restore code and conversation"),
               Option("conversation", "Restore conversation"),
               Option("code", "Restore code"), Option("", "Never mind")]


def matches(app: NoetaApp, typed: str) -> list[tuple[str, str, str]]:
    """The slash menu for ``/typed``: built-in and plugin commands whose
    name starts with it (then contains it)."""
    found = [(name, help_) for name, help_ in COMMANDS.items()]
    found += [(c.name, c.description + (f" {c.argument_hint}" if c.argument_hint else ""))
              for c in app.plugin_commands if c.name not in COMMANDS]
    want = typed.lower()
    first = [f for f in found if f[0].startswith(want)]
    then = [f for f in found if want in f[0] and f not in first]
    return [(f"/{name}", f"/{name}", help_) for name, help_ in first + then]


async def run(app: NoetaApp, name: str, arg: str) -> bool:
    """Run ``/name arg`` if it is one of :data:`COMMANDS`; ``False`` if not."""
    handler = globals().get(f"_{name.replace('-', '_')}")
    if name not in COMMANDS or handler is None:
        return False
    await handler(app, arg.strip())
    return True


def _rows(title: str, rows: list[tuple[str, str]]) -> Text:
    out = Text().append(title + "\n", style="bold")
    width = max((len(k) for k, _ in rows), default=0)
    for k, v in rows:
        out.append(f"  {k.ljust(width)}  ", style="dim")
        out.append(render.clean(f"{v}") + "\n")
    return out


async def _help(app: NoetaApp, arg: str) -> None:
    out = Text().append(f"noeta {app.start.version}".rstrip() + "\n\n", style="bold")
    out.append("Shortcuts\n", style="bold")
    out.append(Text.from_markup(_shortcuts()))
    out.append("\n\nCommands\n", style="bold")
    for name, help_ in COMMANDS.items():
        out.append(f"  /{name:<16}", style=render.ACCENT)
        out.append(f"{help_}\n", style="dim")
    if app.plugin_commands:
        out.append("\nFrom plugins\n", style="bold")
        for c in app.plugin_commands:
            out.append(f"  /{c.name:<16}", style=render.ACCENT)
            out.append(render.clean(f"{c.description}"
                                    + (f" {c.argument_hint}" if c.argument_hint else "")) + "\n",
                       style="dim")
    await app.say_text(out)


def _shortcuts() -> str:
    from noeta_tui.app import SHORTCUTS

    return "\n".join("  " + line for line in SHORTCUTS.splitlines())


async def _status(app: NoetaApp, arg: str) -> None:
    rows = [("version", app.start.version or "?"),
            ("task", app.task_id or "(none yet: the first message starts it)"),
            ("agent", app.agent), ("model", app.model), ("effort", app.effort_said()),
            ("permission mode", app.mode or app.permission_mode or "(the plugin's default)"),
            ("working directory", app.task_cwd or SERVER_DIRECTORY),
            ("runs", "on a server" if app.on_server else "in this process")]
    # The task's own value over the one the settings start tasks with.
    named = {name for name, _ in rows}
    await app.say_text(_rows("Status", rows + [r for r in app.start.settings()
                                               if r[0] not in named]))


#: Line marks of a plugin's panel and their colours.
PANEL_MARKS = {"✔": "green", "✘": "red", "△": "yellow"}


async def _plugin_panel(app: NoetaApp, name: str, empty: str) -> None:
    """The plugin command ``/name``'s text as a panel: its first line the
    title (``empty`` when no plugin of the agent offers it)."""
    try:
        typed = await app.local.run_command(app.agent, app._command_context(), f"/{name}")
    except Exception as exc:  # noqa: BLE001 — a plugin's failure is shown, not raised
        await app.say_notice(f"/{name} failed: {exc}")
        return
    title, _, rest = (typed.text if typed is not None else empty).partition("\n")
    out = Text().append(render.clean(title) + "\n", style="bold")
    for line in rest.splitlines():
        out.append(render.clean(f"  {line}") + "\n", style=PANEL_MARKS.get(line[:1], ""))
    await app.say_text(out)


async def _mcp(app: NoetaApp, arg: str) -> None:
    await _plugin_panel(app, "mcp", "No MCP servers configured.")


async def _hooks(app: NoetaApp, arg: str) -> None:
    await _plugin_panel(app, "hooks", "No hooks configured.")


async def _clear(app: NoetaApp, arg: str) -> None:
    await app.new_task()


async def _new(app: NoetaApp, arg: str) -> None:
    await app.new_task()


async def _plugins(app: NoetaApp, arg: str) -> None:
    await plugin_ui.menu(app, arg)


async def _reload_plugins(app: NoetaApp, arg: str) -> None:
    reload = getattr(app.local, "reload_plugins", None)
    if reload is None:
        await app.say_notice("attached to a server: its workers load plugins when they start, "
                             "so restart them (one by one) to reload; /plugins switches "
                             "them per task")
        return
    lines = reload()
    app._load_commands()
    await app.say_notice("plugins reloaded, in effect from the next turn: " + "; ".join(lines))


async def _model(app: NoetaApp, arg: str) -> None:
    if arg:
        await set_model(app, arg)
        return
    options = [Option(alias, alias, f"→ {real}") for alias, real in models.ALIASES.items()]
    options += [Option(model, model, provider) for model, provider in
                await model_choices(app.local)]
    current = next((n for n, o in enumerate(options) if o.id == app.model), 0)

    async def picked(model: str) -> None:
        app._box().focus()
        if model:
            await set_model(app, model)

    await _picker(app, Picker(f"Select model (now {app.model})", options, picked,
                              index=current), "/model")


async def set_model(app: NoetaApp, name: str) -> None:
    real = models.resolve_alias(name)
    note = await prepare_model(app.local, real)
    await _change(app, {"model": real})
    app.model = real
    app.update_status()
    await app.say_notice(f"model set to {real} from the next request on"
                         + (f" — {note}" if note else ""))


#: The effort levels ``/effort`` offers, lowest first.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


async def _effort(app: NoetaApp, arg: str) -> None:
    if arg:
        await set_effort(app, arg)
        return
    options = [Option(level, level) for level in EFFORTS]
    current = app.effort_level() or ""
    index = EFFORTS.index(current) if current in EFFORTS else 0

    async def picked(level: str) -> None:
        app._box().focus()
        if level:
            await set_effort(app, level)

    await _picker(app, Picker(f"Select effort (now {app.effort_said()})", options,
                              picked, index=index), "/effort")


async def set_effort(app: NoetaApp, level: str) -> None:
    if level not in EFFORTS:
        await app.say_notice(f"effort is one of {', '.join(EFFORTS)}")
        return
    await _change(app, {"effort": level})
    app.effort = level
    app.update_status()
    await app.say_notice(f"effort set to {level} from the next request on")


async def _change(app: NoetaApp, changes: dict[str, str]) -> None:
    """The task's config changed; a screen with no task yet starts its task with it."""
    if app.task_id:
        await change_config(app.local.client, app.task_id, changes)
    else:
        app.task_config = {**app.task_config, **changes}


async def _picker(app: NoetaApp, picker: Picker, what: str = "that") -> None:
    """``picker`` in place of the prompt; a failure of what its pick does
    is shown as ``<what> failed: …``."""
    picked = picker.on_pick_cb

    async def shown(choice: str) -> None:
        async with app.failures_shown(what):
            await picked(choice)

    picker.on_pick_cb = shown
    overlay = app.query_one("#overlay")
    await overlay.remove_children()
    await overlay.mount(picker)


async def _context(app: NoetaApp, arg: str) -> None:
    found = await app.local.context(await app.ensure_task())
    if found is None:
        await app.say_notice("this task's agent is not one this front end runs")
        return
    await app.say_text(render.context_grid(found))


async def _cost(app: NoetaApp, arg: str) -> None:
    if app.task_id:
        total = await tree_usage(app.local.client, app.task_id)
        await app.say_text(Text(cost_text(total)))


async def _copy(app: NoetaApp, arg: str) -> None:
    if arg and not (arg.isdigit() and int(arg) >= 1):
        await app.say_notice("/copy copies the last reply, /copy N the Nth-latest")
        return
    n = int(arg or 1)
    replies = await _replies(app)
    if len(replies) < n:
        await app.say_notice("no reply to copy yet" if not replies else
                             f"there {'is' if len(replies) == 1 else 'are'} only "
                             f"{len(replies)} {'reply' if len(replies) == 1 else 'replies'}")
        return
    text = replies[-n]
    clipboard.copy(app, text)
    which = "the last reply" if n == 1 else f"the {_ordinal(n)}-latest reply"
    await app.say_notice(f"Copied {which} ({len(text)} character{'' if len(text) == 1 else 's'})")


async def _replies(app: NoetaApp) -> list[str]:
    """What the model said in the task's replies, oldest first (a reply of
    tool calls only says nothing)."""
    if not app.task_id:
        return []
    return [said for _, records in await lineage(app.local.client, app.task_id)
            for rec in records if isinstance(rec.event, ModelReplied)
            and (said := rec.event.response.message.text.strip())]


def _ordinal(n: int) -> str:
    """``2nd``, ``3rd``, ``11th``, ``21st``."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def _permissions(app: NoetaApp, arg: str) -> None:
    mode = app.mode or app.permission_mode or ""
    rows = [("mode", f"{mode} — {MODE_NAMES.get(mode, '')}".rstrip(" —") or "(not recorded)"),
            ("cycle", " → ".join(app.modes.cycle) or "(the task's plugin publishes it)")]
    rules = [(k.split(".", 1)[1], v) for k, v in app.start.settings()
             if k.startswith("permissions.")]
    out = _rows("Permissions", rows + (rules or [("rules", "(none in the settings files)")]))
    out.append("  shift+tab switches the mode; /config edit user|project|local edits the rules",
               style="dim")
    await app.say_text(out)


async def _agents(app: NoetaApp, arg: str) -> None:
    names = await app.local.agent_names()
    out = Text().append("Agents\n", style="bold")
    for name in names:
        out.append(f"  {'❯' if name == app.agent else ' '} {name}\n",
                   style="bold" if name == app.agent else "")
    out.append("  agent files: ~/.noeta/agents/*.md, .noeta/agents/*.md; noeta --agent NAME "
               "starts with another", style="dim")
    await app.say_text(out)


async def _memory_dir(app: NoetaApp) -> str | None:
    if not app.task_id:
        return None
    for rec in await app.local.client.events(app.task_id):
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == MEMORY \
                and ev.kind == "dir" and isinstance(ev.data, dict):
            return str(ev.data.get("dir") or "") or None
    return None


def _home(app: NoetaApp) -> Path:
    return Path(app.start.home) if app.start.home else Path.home() / ".noeta"


async def memory_files(app: NoetaApp) -> list[tuple[str, Path]]:
    """The instruction and memory files /memory offers, existing or not."""
    cwd = Path(app.cwd)
    files = [("user instructions", _home(app) / "NOETA.md"),
             ("project instructions", cwd / "NOETA.md"),
             ("project instructions (.noeta)", cwd / ".noeta" / "NOETA.md"),
             ("local instructions (not checked in)", cwd / "NOETA.local.md")]
    memory = await _memory_dir(app)
    if memory:
        files.append(("auto memory index", Path(memory) / "MEMORY.md"))
    return files


def open_in_editor(app: NoetaApp, path: Path) -> str | None:
    """Edit ``path`` in ``$VISUAL`` / ``$EDITOR`` with the UI suspended; the
    reason when that could not be done."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        return "set $EDITOR to edit files from here"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with app.suspend():
            subprocess.run([*shlex.split(editor), str(path)], check=False)
    except Exception as exc:  # noqa: BLE001 — a terminal that cannot suspend
        return f"could not open the editor: {exc}"
    return None


async def _memory(app: NoetaApp, arg: str) -> None:
    files = await memory_files(app)
    if arg.isdigit() and 1 <= int(arg) <= len(files):
        _, path = files[int(arg) - 1]
        problem = open_in_editor(app, path)
        await app.say_notice(problem or f"edited {path}; it is read when a task starts")
        return
    lines = [f"{n}. {label}: `{path}`" + ("" if path.exists() else " (not created yet)")
             for n, (label, path) in enumerate(files, 1)]
    await app.say_markdown("**Memory**\n" + "\n".join(lines) + "\n\n`/memory <n>` opens one.")


async def _config(app: NoetaApp, arg: str) -> None:
    words = arg.split()
    if words[:1] == ["edit"]:
        where = words[1] if len(words) > 1 else "user"
        target = {"user": _home(app) / "settings.json",
                  "project": Path(app.cwd) / ".noeta" / "settings.json",
                  "local": Path(app.cwd) / ".noeta" / "settings.local.json"}.get(where)
        if target is None:
            await app.say_notice("edit which: user, project or local")
            return
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n", "utf-8")
        problem = open_in_editor(app, target)
        await app.say_notice(problem or f"edited {target}; it applies to tasks started from now")
        return
    rows = [("model", app.model),
            ("permission mode", app.mode or app.permission_mode or "bypassPermissions"),
            ("working directory", app.task_cwd or SERVER_DIRECTORY), *app.start.settings()]
    out = _rows("Config", rows)
    out.append("  /config edit user|project|local opens a settings file", style="dim")
    await app.say_text(out)


def _point_label(point: RewindPoint) -> str:
    first = point.text.splitlines()[0][:80] if point.text else "(empty)"
    return first + (f" — {len(point.files)} file(s) changed since" if point.files else "")


async def _rewind(app: NoetaApp, arg: str) -> None:
    if not app.task_id:
        return
    points = await rewind_points(app.local.client, app.task_id)
    if not points:
        await app.say_notice("nothing to rewind to yet")
        return
    words = arg.split()
    if not words or not words[0].isdigit():
        options = [Option(str(n), _point_label(p), _ago(p.at))
                   for n, p in enumerate(points, 1)]

        async def picked_point(n: str) -> None:
            if not n:
                app._box().focus()
                return

            async def picked_what(what: str) -> None:
                app._box().focus()
                if what:
                    await rewind_to(app, points, int(n), what)

            await _picker(app, Picker(f"Rewind to before: {_point_label(points[int(n) - 1])}",
                                      REWIND_WHAT, picked_what), "/rewind")

        await _picker(app, Picker("Rewind — restore the code and/or conversation to before "
                                  "a prompt", options, picked_point, index=len(options) - 1),
                      "/rewind")
        return
    n = int(words[0])
    if not 1 <= n <= len(points):
        await app.say_notice(f"pick 1 to {len(points)}")
        return
    what = words[1] if len(words) > 1 else "both"
    if what not in ("both", "conversation", "code"):
        await app.say_notice("rewind what: both, conversation or code")
        return
    await rewind_to(app, points, n, what)


async def rewind_to(app: NoetaApp, points: list[RewindPoint], n: int, what: str) -> None:
    """The files now (by the task rewound); the conversation is shown up to
    the point at once, its task made with the first message sent."""
    point = points[n - 1]
    files: tuple[str, ...] = ()
    if what != "conversation":
        files = (await app.local.rewind(app.task_id, point, conversation=False,
                                        files=True)).files
    if what != "code":
        await app.open_fork(app.task_id, point)
        app.prefill(point.text)
    restored = f"; {len(files)} file(s) restored" if files else ""
    await app.say_notice(f"rewound to before prompt {n}{restored}")


def _ago(at: float) -> str:
    return render.elapsed_text(max(time.time() - at, 0)) + " ago" if at else ""


#: Conversations the resume list reads at most, the most recently updated.
RESUME_LIMIT = 200
#: The resume list's choice that lists every directory's conversations.
EVERYWHERE = "*everywhere"


@dataclass(frozen=True)
class Listed:
    """A root task as the resume list shows it: where it started, what was
    first said in it (in the conversation it continues, for a rewind's),
    and the task it continues (``""``: none)."""

    row: Any
    cwd: str
    prompt: str
    continues: str


async def _listed(app: NoetaApp, row: Any) -> Listed:
    from noeta_tui.app import first_prompt

    try:
        events = await app.local.client.events(row.task_id)
    except Exception:  # noqa: BLE001 — a listing shows what it can
        return Listed(row, "", "", "")
    first = events[0].event if events else None
    created = first if isinstance(first, TaskCreated) else None
    source = str(created.history_from or "") if created is not None else ""
    prompt = ""
    if source:
        with contextlib.suppress(Exception):
            prompt = next((said for _, records in await lineage(app.local.client, row.task_id)
                           if (said := first_prompt(records))), "")
    return Listed(row, str(created.config.get("cwd") or "") if created is not None else "",
                   prompt or first_prompt(events), source)


async def resume_picker(app: NoetaApp, *, everywhere: bool = False) -> None:
    """``/resume``: the conversations to pick from, the latest first, each
    with its first prompt, as Claude Code lists them: those started in this
    directory (a last choice lists every directory's), one conversation
    per line however often it was rewound (a rewind's continuation stands
    for what it continues). Listing changes nothing: only the task picked
    is opened."""
    rows = await app.local.client.tasks(roots_only=True, limit=RESUME_LIMIT)
    listed = list(await asyncio.gather(*(_listed(app, r) for r in rows)))
    here = str(app.task_config.get("cwd") or ("" if app.on_server else app.cwd))
    if here and not everywhere:
        others = sum(1 for x in listed if x.cwd != here)
        listed = [x for x in listed if x.cwd == here]
    else:
        others = 0
    continued = {x.continues for x in listed if x.continues}
    listed = [x for x in listed if x.row.task_id not in continued]
    if not listed and not others:
        await app.say_notice("no tasks to resume")
        return
    app.listed = [x.row.task_id for x in listed]
    options = []
    for x in listed:
        detail = f"{_ago(x.row.updated_at)} · {x.row.status} · {x.row.task_id[:8]}"
        if x.continues:
            detail += f" · rewound from {x.continues[:8]}"
        if everywhere and x.cwd:
            detail += f" · {x.cwd}"
        options.append(Option(x.row.task_id, x.prompt.splitlines()[0][:60] if x.prompt
                              else "(no prompt)", detail))
    if others:
        options.append(Option(EVERYWHERE, f"Show conversations from every directory "
                                          f"({others} more)"))

    async def picked(task_id: str) -> None:
        app._box().focus()
        if task_id == EVERYWHERE:
            await resume_picker(app, everywhere=True)
        elif task_id and task_id != app.task_id:
            await app.open_task(task_id)

    title = "Resume a conversation" + ("" if everywhere or not here else " in this directory")
    await _picker(app, Picker(title, options, picked), "/resume")


#: Claude Code's question on first opening a folder.
TRUST_QUESTION = (
    "Accessing workspace:\n\n{cwd}\n\nQuick safety check: Is this a project you created or "
    "one you trust? (Like your own code, a well-known open source project, or work from your "
    "team). If not, take a moment to review what's in this folder first.\n\nnoeta will be "
    "able to read, edit, and execute files here, and load the project's plugins, agents, "
    "prompts, skills and settings.")
TRUST_OPTIONS = [Option("", "No, exit"), Option("trust", "Yes, I trust this folder")]


async def trust_picker(app: NoetaApp, then: Callable[[], Awaitable[None]]) -> None:
    """The folder is not trusted: ask before anything runs in it; ``then``
    once it is, else quit."""
    trust = app.start.trust
    assert trust is not None

    async def picked(choice: str) -> None:
        if not choice:
            app.exit()
            return
        await asyncio.to_thread(trust)
        await then()

    await _picker(app, Picker(TRUST_QUESTION.format(cwd=app.cwd), TRUST_OPTIONS, picked,
                              hint="Enter to confirm · Esc to cancel"))
