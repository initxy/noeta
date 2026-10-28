"""The terminal UI's own slash commands with a mechanism behind them, as
Claude Code has them: ``/model``, ``/context``, ``/cost``, ``/memory``,
``/config``, ``/rewind``, ``/clear``. Each takes the app and what was typed
after the name; the text it shows goes to the transcript, never to the
model."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.core import models
from noeta.core.events import ExtensionRecorded
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.host.rewind import rewind, rewind_points
from noeta.host.usage import context_breakdown, context_text, cost_text, tree_usage

if TYPE_CHECKING:
    from noeta.cli.tui.app import NoetaApp

__all__ = ["COMMANDS", "run"]

#: name -> what /help says.
COMMANDS = {
    "model": "`[name]` show or switch the model (from the next request on)",
    "context": "what fills the next request",
    "cost": "tokens and cost of this task and its sub-agents",
    "memory": "`[n]` list instruction and memory files; open one in $EDITOR",
    "config": "`[edit user|project|local]` show the settings; edit a settings file",
    "rewind": "`[n] [both|conversation|code]` go back to before a prompt (also Esc Esc)",
    "clear": "start a new task (`/new`)",
    "reload-plugins": "load the plugin and agent files again (from the next turn on)",
}


async def run(app: NoetaApp, name: str, arg: str) -> bool:
    """Run ``/name arg`` if it is one of :data:`COMMANDS`; ``False`` if not."""
    handler = globals().get(f"_{name.replace('-', '_')}")
    if name not in COMMANDS or handler is None:
        return False
    await handler(app, arg.strip())
    return True


async def _clear(app: NoetaApp, arg: str) -> None:
    await app.new_task()


async def _reload_plugins(app: NoetaApp, arg: str) -> None:
    from noeta.cli.config import plugin_changes
    from noeta.core import registry

    reload = getattr(app.local, "reload_plugins", None)
    if reload is None:
        await app.say_notice("attached to a server: its workers load plugins when they start, "
                             "so restart them (one by one) to reload")
        return
    before = dict(app.local.runtime.agents)
    notices = reload()
    changes = plugin_changes(before, app.local.runtime.agents) or ["no plugin changed"]
    failed = [f"failed: {p}: {why}" for p, why in registry.broken_plugin_files().items()]
    await app.say_notice("plugins reloaded, in effect from the next turn: "
                         + "; ".join([*changes, *failed, *notices]))


async def _model(app: NoetaApp, arg: str) -> None:
    if not arg:
        aliases = ", ".join(f"`{a}` ({r})" for a, r in models.ALIASES.items())
        await app.say_markdown(f"Model: `{app.model}`. Switch with `/model <name>`; "
                               f"aliases: {aliases}.")
        return
    if not app.task_id:
        return
    real = models.resolve_alias(arg)
    await app.local.client.signal(app.task_id, CONFIG_SIGNAL, {"model": real})
    app.model = real
    app.update_status()
    note = models.unknown_model_notice(real)
    await app.say_notice(f"model set to {real} from the next request on"
                         + (f" — {note}" if note else ""))


async def _context(app: NoetaApp, arg: str) -> None:
    if not app.task_id:
        return
    runtime = getattr(app.local, "runtime", None)
    if runtime is not None:
        preview = await runtime.preview(app.task_id)
        if preview is None:
            await app.say_notice("this task's agent is not one this front end runs")
            return
        parts = context_breakdown(preview)
    else:
        remote: Any = app.local.client
        parts = await remote.context(app.task_id)
    await app.say_markdown(f"```\n{context_text(parts)}\n```")


async def _cost(app: NoetaApp, arg: str) -> None:
    if app.task_id:
        total = await tree_usage(app.local.client, app.task_id)
        await app.say_markdown(f"```\n{cost_text(total)}\n```")


async def _memory_dir(app: NoetaApp) -> str | None:
    if not app.task_id:
        return None
    for rec in await app.local.client.events(app.task_id):
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == "memory" \
                and ev.kind == "dir" and isinstance(ev.data, dict):
            return str(ev.data.get("dir") or "") or None
    return None


async def memory_files(app: NoetaApp) -> list[tuple[str, Path]]:
    """The instruction and memory files /memory offers, existing or not."""
    from noeta.cli.config import HOME

    cwd = Path(app.cwd)
    files = [("user instructions", HOME / "NOETA.md"),
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
    from noeta.cli.config import HOME, file_settings

    words = arg.split()
    if words[:1] == ["edit"]:
        where = words[1] if len(words) > 1 else "user"
        target = {"user": HOME / "settings.json",
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
    s: Any = app.settings
    found = file_settings(app.cwd)
    rows = [("model", app.model), ("permission mode", app.mode or app.permission_mode or "auto"),
            ("working directory", app.cwd)]
    if s is not None:
        rows += [("provider", s.provider), ("base URL", s.base_url or "(default)"),
                 ("store", s.store_url), ("effort", s.effort or "(default)")]
    rows += [("settings files", ", ".join(str(f) for f in found.files) or "(none)")]
    perms = found.permissions
    for key in ("allow", "deny", "ask"):
        if perms.get(key):
            rows.append((f"permissions.{key}", ", ".join(perms[key])))
    table = "\n".join(f"- **{k}**: `{v}`" for k, v in rows)
    await app.say_markdown(f"**Config**\n{table}\n\n`/config edit user|project|local` opens a "
                           "settings file.")


async def _rewind(app: NoetaApp, arg: str) -> None:
    if not app.task_id:
        return
    points = await rewind_points(app.local.client, app.task_id)
    if not points:
        await app.say_notice("nothing to rewind to yet")
        return
    words = arg.split()
    if not words or not words[0].isdigit():
        lines = [f"{n}. {p.text.splitlines()[0][:80] if p.text else '(empty)'}"
                 + (f" — {len(p.files)} file(s) changed since" if p.files else "")
                 for n, p in enumerate(points, 1)]
        await app.say_markdown("**Rewind** to before a prompt: `/rewind <n> [both|conversation"
                               "|code]`\n" + "\n".join(lines))
        app.prefill("/rewind ")
        return
    n = int(words[0])
    if not 1 <= n <= len(points):
        await app.say_notice(f"pick 1 to {len(points)}")
        return
    what = words[1] if len(words) > 1 else "both"
    if what not in ("both", "conversation", "code"):
        await app.say_notice("rewind what: both, conversation or code")
        return
    point = points[n - 1]
    done = await rewind(app.local.client, app.task_id, point,
                        conversation=what != "code", files=what != "conversation")
    if done.task_id != app.task_id:
        await app.open_task(done.task_id, resumed=False)
        app.prefill(done.prompt)
    restored = f"; {len(done.files)} file(s) restored" if done.files else ""
    await app.say_notice(f"rewound to before prompt {n}{restored}")
