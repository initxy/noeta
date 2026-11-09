"""What every front end (the TUI, ACP, stream-json) runs on: the client
plus what only the side running the agents can answer — the agents' names,
their slash commands, and what fills a task's next request. ``LocalApp``
answers it in process, ``RemoteApp`` over HTTP from a ``noeta serve``
server, so a front end is the same code on one machine or against many."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from noeta.core import models
from noeta.core.agent import Agent
from noeta.core.client import ClientAPI
from noeta.core.commands import Typed, list_commands, run_typed
from noeta.core.extension import Command, CommandContext
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.types import Delta
from noeta.host.plugins import MarketEntry, PluginInfo, UiEntry
from noeta.host.rewind import RewindPoint, Rewound
from noeta.toolkit.signals import ARCHIVE_SIGNAL

DeltaSink = Callable[[str, Delta], None]

log = logging.getLogger("noeta.host.app")


class AppAPI(Protocol):
    @property
    def client(self) -> ClientAPI: ...

    def deltas(self, sink: DeltaSink) -> Callable[[], None]:
        """Call ``sink(task_id, delta)`` with live model output; returns the
        unsubscribe function."""
        ...

    async def adopt(self, task_id: str) -> None:
        """Make sure the tree of an existing task is served (a resume)."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def agent_names(self) -> list[str]: ...

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        """The slash commands ``agent``'s plugins offer (none for an agent
        this side does not run); with ``ctx.task_id``, only the plugins
        that task runs with now."""
        ...

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        """``text`` (``/name args``) run as ``agent``'s command; ``None``
        when no plugin of it offers that name."""
        ...

    async def plugins(self, agent: str, cwd: str = "") -> list[PluginInfo]:
        """The plugins ``agent`` runs here and the installed ones it does
        not (``noeta.host.plugins.plugin_infos``)."""
        ...

    async def set_plugin_default(self, name: str, enabled: bool) -> None:
        """Switch the plugin ``name`` on or off for the tasks created from
        now on (a task's own switch is ``change_config`` with
        ``plugins_off``). Raises ``ValueError`` for a plugin no task may
        run without."""
        ...

    async def ui(self, task_id: str) -> list[UiEntry]:
        """What the task's plugins show now (``Extension.ui``)."""
        ...

    async def plugin_call(self, task_id: str, plugin: str, name: str, args: Any) -> Any:
        """``plugin``'s answer to its front-end code (``Extension.ui_call``);
        raises ``UnknownUiCall`` when the task does not run it or it has no
        such call."""
        ...

    async def install_plugin(self, source: str, marketplace: str | None = None,
                             commit: str | None = None) -> PluginInfo:
        """Install a plugin as ``noeta plugin add`` does, then load it."""
        ...

    async def remove_plugin(self, name: str) -> None:
        """Remove a plugin as ``noeta plugin remove`` does, then reload."""
        ...

    async def market(self, notices: list[str] | None = None) -> list[MarketEntry]:
        """The configured marketplaces' plugins; a marketplace that could not
        be read (left out, or listed as last read) adds a line to
        ``notices``."""
        ...

    async def context(self, task_id: str) -> dict[str, Any] | None:
        """What fills the task's next request
        (``noeta.host.usage.context_breakdown``); ``None`` when this side
        does not run the task's agent."""
        ...

    async def summaries(self, limit: int = 50, *,
                        archived: bool | None = None) -> list[dict[str, Any]]:
        """The root tasks to resume from, newest first
        (``noeta.host.summaries.root_summaries``): only the archived ones
        (``archived=True``), only the others (``False``) or all."""
        ...

    async def archive(self, task_id: str, archived: bool = True) -> None:
        """Archive the task (the session lists leave it out), or with
        ``archived=False`` bring it back (:func:`archive_task`)."""
        ...

    async def rewind(self, task_id: str, point: RewindPoint, *, conversation: bool = True,
                     files: bool = True) -> Rewound:
        """Rewind ``task_id`` to before ``point`` (``noeta.host.rewind``):
        in process with :func:`noeta.host.rewind.rewind`, against a server
        through its ``POST /tasks/{id}/rewind``, which makes the new task
        with only the config a request may set."""
        ...


@dataclass
class FrontEndStart:
    """What the ``noeta`` command hands a front end it opens: one found by
    name in the entry-point group ``noeta.frontends`` (``tui``), called
    ``run(app, start)`` with the :class:`AppAPI` it built, returning when
    the person quits."""

    #: The agent new tasks run.
    agent: str
    #: The directory the person started in (on this machine).
    cwd: str
    #: The model new tasks run, as the person reads it.
    model: str
    #: What each new task's config starts with (a server's own choices
    #: when empty).
    task_config: dict[str, Any] = field(default_factory=dict)
    #: A task to open; ``""``: let the person pick one; ``None``: a new one.
    resume: str | None = None
    #: Without ``resume``: open the newest task started in ``cwd`` (``-c``).
    continue_last: bool = False
    #: The permission mode new tasks start in (``None``: the plugin's default).
    permission_mode: str | None = None
    #: ``app`` is a server's: tasks run in its directories, not ``cwd``.
    on_server: bool = False
    #: The noeta home directory (``~/.noeta``: its settings and ``NOETA.md``).
    home: str = ""
    #: ``(name, value)`` rows of the settings in effect, for showing.
    settings: Callable[[], list[tuple[str, str]]] = lambda: []
    #: The installed noeta's version.
    version: str = ""
    #: Set when the project at ``cwd`` is not trusted: trusts it and loads
    #: what only a trusted project loads (the front end asks first).
    trust: Callable[[], None] | None = None
    #: The person's first message (``noeta "<prompt>"``), sent as if typed
    #: once the task opens.
    prompt: str | None = None
    #: The warnings logged since the start, for the front end to show: its
    #: screen covers stderr, where they would otherwise go.
    warnings: LoggedWarnings | None = None


class LoggedWarnings(logging.Handler):
    """Warnings and errors logged while a front end's screen covers stderr,
    each as one line: held until :meth:`show_with` names where they go, then
    handed there as they come, each text once. Records of the task runner
    (``noeta.core.runner``) are left out: a front end shows a failed task or
    tool from the task's own events."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self._held: list[tuple[str, int]] = []
        self._seen: set[str] = set()
        self._show: Callable[[str, int], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("noeta.core.runner"):
            return
        try:
            line = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string is still a line
            line = str(record.msg)
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and str(exc) and str(exc) not in line:
            line += f": {exc}"
        with self.lock:  # type: ignore[union-attr]
            if line in self._seen:
                return
            self._seen.add(line)
            show = self._show
            if show is None:
                self._held.append((line, record.levelno))
                return
        show(line, record.levelno)

    def forget(self, lines: Collection[str]) -> None:
        """Never show ``lines`` (what the front end is given as notices)."""
        with self.lock:  # type: ignore[union-attr]
            self._seen.update(lines)
            self._held = [h for h in self._held if h[0] not in lines]

    def show_with(self, show: Callable[[str, int], None], *,
                  shown: Collection[str] = ()) -> None:
        """Hand the held lines, then every later one, to ``show(line,
        level)`` (from whichever thread logs it); ``shown``: lines the front
        end shows already."""
        with self.lock:  # type: ignore[union-attr]
            self._seen.update(shown)
            held, self._held, self._show = self._held, [], show
        for line, level in held:
            if line not in shown:
                show(line, level)


async def change_config(client: ClientAPI, task_id: str, changes: Mapping[str, Any]) -> None:
    """Change a task's config from its next model call on: ``model``,
    ``effort`` and the kernel's other changeable keys; ``None`` removes one."""
    await client.signal(task_id, CONFIG_SIGNAL, dict(changes))


async def archive_task(client: ClientAPI, task_id: str, archived: bool = True) -> None:
    """Post the ``archive`` signal: an ended task takes it too, since the
    session list reads it from the inbox (so it wakes no idle task)."""
    await client.signal(task_id, ARCHIVE_SIGNAL, {"archived": archived}, wake=False)


async def agent_commands(agents: Mapping[str, Agent], agent: str, ctx: CommandContext,
                         off: Collection[str] = ()) -> list[Command]:
    """``agent``'s commands, less the ones of the plugins in ``off``."""
    found = agents.get(agent)
    return await list_commands(found, ctx, off) if found is not None else []


async def agent_run_command(agents: Mapping[str, Agent], agent: str, ctx: CommandContext,
                            text: str, off: Collection[str] = ()) -> Typed | None:
    found = agents.get(agent)
    if found is None:
        return None
    return await run_typed([e for e in found.all_extensions() if e.name not in off], ctx, text)


async def model_choices(app: Any, *, catalog: bool = True) -> list[tuple[str, str]]:
    """``(model, provider name)`` for the models a front end offers: what
    the app's providers serve (``LocalApp.list_models``), else (``catalog``)
    the catalog's, with no provider name."""
    lister = getattr(app, "list_models", None)
    found: list[tuple[str, str]] = []
    if lister is not None:
        try:
            found = await asyncio.to_thread(lister)
        except Exception:
            log.warning("listing the providers' models failed", exc_info=True)
    if found or not catalog:
        return found
    return [(m, "") for m in models.catalog_models()]


async def prepare_model(app: Any, model: str) -> str | None:
    """Settle ``model`` before a switch to it (``LocalApp.prepare_model``):
    what the person should know about it, or ``None``."""
    prepare = getattr(app, "prepare_model", None)
    if prepare is None:
        return models.unknown_model_notice(model)
    result: str | None = await asyncio.to_thread(prepare, model)
    return result
