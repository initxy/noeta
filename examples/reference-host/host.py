"""Reference host — the smallest complete Noeta embedding host.

Demonstrated SDK capability
---------------------------
Everything a host owns, assembled from the public surface alone (``noeta.sdk``
/ ``noeta.sdk.storage`` / ``noeta.presets``): durable sqlite storage injected
through :class:`~noeta.sdk.HostConfig`, a token-streaming ``delta_sink``,
manifest plugins loaded and wired by their effect scope, and the official
``main`` agent recipe. It reaches no runtime internal, so anything it can build
a third-party host can build too.

:func:`build_reference_host` takes the provider as an argument rather than
constructing one, so a production host swaps that single argument and changes
nothing else. The offline :func:`main` demo passes a scripted streaming double,
which is what lets the file run with no API key and no network::

    python examples/reference-host/host.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, TextIO

from noeta.sdk import (
    Client,
    HostConfig,
    LLMRequest,
    LLMResponse,
    Options,
    PluginSet,
    StepContext,
    StreamDelta,
    TextBlock,
    Usage,
    load_plugins,
    presets,
)
from noeta.sdk.storage import (
    SqliteContentStore,
    SqliteDispatcher,
    SqliteEventLog,
)


__all__ = [
    "ReferenceHost",
    "StdoutDeltaSink",
    "build_reference_host",
    "default_plugin_modules",
    "default_activation",
    "apply_plugin_env",
    "plugin_env_scope",
    "main",
]


# ---------------------------------------------------------------------------
# The streaming sink (HostConfig.delta_sink)
# ---------------------------------------------------------------------------


class StdoutDeltaSink:
    """A ``HostConfig.delta_sink`` that writes streamed token text to ``out``.

    ``delta_count`` exists so a host — or a test — can confirm the streaming
    path was taken rather than the batch one.
    """

    def __init__(self, out: Optional[TextIO] = None) -> None:
        self._out: TextIO = out if out is not None else sys.stdout
        self.delta_count = 0

    def __call__(
        self, ctx: StepContext, call_id: str, delta: StreamDelta
    ) -> None:
        try:
            # Both delta kinds land on one stream, which is only acceptable for
            # a demo: a product gives ``thinking`` (the model's private
            # reasoning preview) its own pane rather than interleaving it with
            # the answer text.
            self._out.write(delta.text)
            self._out.flush()
        except Exception:
            # Deltas are previews of content the turn records anyway, so a slow
            # or broken output stream must never fail the LLM call.
            return
        self.delta_count += 1


# ---------------------------------------------------------------------------
# Plugin discovery (explicit-path loading of examples/plugins/*)
# ---------------------------------------------------------------------------

#: The example plugins this host wires. The first three contribute governance
#: surfaces and take effect for every agent the moment they are loaded; the last
#: three contribute per-agent surfaces, which fire only for an agent that opts
#: in, so they appear again in :func:`default_activation`.
_WIRED_PLUGINS: tuple[str, ...] = (
    "protected-paths",
    "approval-modes",
    "git-checkpoint",
    "redaction",
    "checklist-reminder",
    "memory-recall",
)


def default_plugin_modules() -> list[str]:
    """The wired example plugins' ``plugin.py`` paths.

    A deployed host discovers plugins through entry points; the example plugins
    are not installed, so they are loaded by explicit file path instead — the
    ``modules=[...]`` seam of :func:`~noeta.sdk.load_plugins`. Load order never
    affects the compiled ``AgentSpec``; the sort is only there to keep the
    discovered set stable between runs.
    """
    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    return sorted(str(plugins_dir / name / "plugin.py") for name in _WIRED_PLUGINS)


def default_activation() -> tuple[str, ...]:
    """The loaded plugins the main agent activates.

    A per-agent surface — ``redaction``'s ``tool_result_transform``,
    ``checklist-reminder``'s compose-time ``reminder``, ``memory-recall``'s
    recorded ``reminder_provider`` — does nothing until an agent opts in.
    Governance guards and observers need no such opt-in.
    """
    return ("redaction", "checklist-reminder", "memory-recall")


def apply_plugin_env(workspace_dir: Path) -> dict[str, str]:
    """Point the example plugins at ``workspace_dir`` through the environment.

    A manifest keeps plugin config orthogonal to agent identity: resolution
    turns a contribution's ``ref`` into a live object and never threads a
    per-plugin config dict. So a host configures a plugin the 12-factor way, and
    must do it **before** the plugin modules are imported — that import is when
    they read their settings.

    The variables stay set for the rest of the process, which is what a host
    that configures itself once at startup wants. A caller that builds several
    hosts in one process needs :func:`plugin_env_scope` instead: a variable
    pointing at a per-build temporary directory outlives that directory, so the
    next build silently reads a path that is gone.
    """
    env = _plugin_env(workspace_dir)
    os.environ.update(env)
    return env


@contextmanager
def plugin_env_scope(workspace_dir: Path) -> Iterator[dict[str, str]]:
    """:func:`apply_plugin_env` for the duration of a block, then restored.

    The plugin modules read their config at import, so the variables only need
    to be in force while :func:`~noeta.sdk.load_plugins` runs. Restoring on exit
    keeps one build's workspace path out of the next build's environment.
    """
    env = _plugin_env(workspace_dir)
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield env
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _plugin_env(workspace_dir: Path) -> dict[str, str]:
    return {
        "NOETA_PROTECTED_PATHS_ROOTS": str(workspace_dir),
        "NOETA_GIT_CHECKPOINT_REPO": str(workspace_dir),
    }


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------


@dataclass
class ReferenceHost:
    """A live, durable, plugin-extended Noeta session driver.

    Built by :func:`build_reference_host`. It keeps a handle on the sqlite
    triple alongside the client because those stores are the host's to close —
    see :meth:`close`.
    """

    client: Client
    options: Options
    plugins: PluginSet
    db_path: Path
    delta_sink: StdoutDeltaSink
    _event_log: SqliteEventLog
    _content_store: SqliteContentStore
    _dispatcher: SqliteDispatcher

    def guard_names(self) -> list[str]:
        """The loaded plugins' guards — in force with or without activation."""
        guards, _observers = self.plugins.process_hooks()
        return sorted(getattr(g, "name", type(g).__name__) for g in guards)

    def observer_names(self) -> list[str]:
        """The loaded plugins' observers — in force with or without activation."""
        _guards, observers = self.plugins.process_hooks()
        return sorted(getattr(o, "name", type(o).__name__) for o in observers)

    def run(self, goal: str) -> Any:
        """Create the session (if new) and drive one turn to its trailing stop.

        A turn that finishes normally comes back ``suspended`` on the next-goal
        handle rather than terminal: the session stays alive for the next
        message, and the answer is already durable by the time this returns.
        """
        return self.client.start(goal=goal)

    def messages(self, task_id: str) -> list[Any]:
        """The human-readable message view of ``task_id``."""
        return self.client.messages(task_id)

    def events(self, task_id: str) -> list[Any]:
        """The durable event-envelope stream for ``task_id``."""
        return self.client.events(task_id)

    def close(self) -> None:
        """Shut the client down, then close the sqlite triple.

        ``Client.shutdown`` reaps runtime resources but leaves injected stores
        open — they belong to whoever constructed them, which here is the host.
        """
        try:
            self.client.shutdown()
        finally:
            for store in (self._event_log, self._content_store, self._dispatcher):
                try:
                    store.close()
                except Exception:
                    pass


def build_reference_host(
    *,
    provider: Any,
    workspace_dir: Path,
    db_path: Optional[Path] = None,
    plugin_modules: Optional[Sequence[str]] = None,
    activate: Optional[Sequence[str]] = None,
    base_options: Optional[Options] = None,
    delta_out: Optional[TextIO] = None,
) -> ReferenceHost:
    """Assemble a :class:`ReferenceHost` from the public surface only.

    ``provider`` is injected rather than constructed here, so one builder serves
    both the offline demo and a production host; it needs to satisfy
    ``LLMProvider``, and ``StreamingProvider`` too if the token stream is to
    carry anything. ``workspace_dir`` is the fence the fs and shell tools are
    confined to.

    The remaining arguments exist so a caller can narrow the host. They fall
    back to :func:`default_plugin_modules`, :func:`default_activation`,
    :func:`noeta.presets.main_options`, ``sys.stdout``, and a throwaway sqlite
    file under a temporary directory.
    """
    if db_path is None:
        db_path = Path(tempfile.mkdtemp(prefix="noeta-reference-host-")) / "noeta.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # One sqlite file backs all three stores, and the dispatcher has to exist
    # first because the event log takes it as its lease_validator.
    dispatcher = SqliteDispatcher(str(db_path))
    event_log = SqliteEventLog(str(db_path), lease_validator=dispatcher)
    content_store = SqliteContentStore(str(db_path))

    # ``builtins=False`` narrows the loaded set to the example plugins — noeta's
    # own capabilities already ride the preset recipe — and loading at build
    # time means a broken plugin fails here rather than mid-session.
    modules = (
        list(plugin_modules)
        if plugin_modules is not None
        else default_plugin_modules()
    )
    with plugin_env_scope(workspace_dir):
        plugins = load_plugins(builtins=False, modules=modules)

    # Activation is additive on top of the recipe's own: replacing that tuple
    # would strip the agent of the built-in capabilities the preset grants it.
    base = base_options if base_options is not None else presets.main_options()
    activation = tuple(activate) if activate is not None else default_activation()
    new_activation = tuple(base.plugins) + tuple(
        name for name in activation if name not in base.plugins
    )
    options = replace(base, plugins=new_activation)

    # Wiring only — none of it reaches the agent identity. Host-plane plugin
    # contributions (mcp_server / skills) would be read off
    # ``plugins.contributions(surface)`` and wired in alongside; the example
    # plugins contribute none.
    delta_sink = StdoutDeltaSink(delta_out)
    host_config = HostConfig(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        delta_sink=delta_sink,
    )

    # Handing the PluginSet to the Client is what routes each contribution by
    # its effect scope: governance to the process-wide stacks, the rest to the
    # agents that activated it.
    client = Client(
        options,
        provider=provider,
        workspace_dir=workspace_dir,
        host_config=host_config,
        plugins=plugins,
    )

    return ReferenceHost(
        client=client,
        options=options,
        plugins=plugins,
        db_path=db_path,
        delta_sink=delta_sink,
        _event_log=event_log,
        _content_store=content_store,
        _dispatcher=dispatcher,
    )


# ---------------------------------------------------------------------------
# Offline demo double — a scripted StreamingProvider (no network)
# ---------------------------------------------------------------------------


class _ScriptedStreamingProvider:
    """Network-free scripted ``StreamingProvider`` for the offline demo.

    Built from the public message types instead of reusing
    ``noeta.testing.fake_llm``, which would break this file's claim to touch
    nothing but the public surface. It streams its one fixed answer as text
    deltas before returning it — the push-shaped contract a real streaming
    adapter implements. A production host deletes this and injects a real
    provider.
    """

    def __init__(self, answer: str = "Hello from the Noeta reference host.") -> None:
        self._answer = answer

    def _response(self) -> LLMResponse:
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text=self._answer)],
            usage=Usage(uncached=1, output=1),
        )

    def _chunks(self) -> list[str]:
        # The fragments must concatenate back to the final content — the
        # invariant a real streaming adapter guarantees, and the one a host's
        # delta sink is entitled to rely on.
        words = self._answer.split(" ")
        return [(w if i == 0 else " " + w) for i, w in enumerate(words)]

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self._response()

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Any,
        request_headers: Optional[dict] = None,
    ) -> LLMResponse:
        for chunk in self._chunks():
            on_delta(StreamDelta(kind="text", text=chunk, index=0))
        return self._response()


def main() -> int:
    """Run one streamed, durable, plugin-extended turn offline and print it."""
    workspace = Path(tempfile.mkdtemp(prefix="noeta-reference-workspace-"))
    host = build_reference_host(
        provider=_ScriptedStreamingProvider(),
        workspace_dir=workspace,
    )
    try:
        print("--- streaming (live token deltas) -------------------------")
        outcome = host.run(goal="Say hello.")
        print()  # end the streamed line
        print("-----------------------------------------------------------")
        print(f"task:          {outcome.task_id}")
        print(f"status:        {outcome.status}")
        print(f"deltas seen:   {host.delta_sink.delta_count}")
        print(f"sqlite file:   {host.db_path}  (exists={host.db_path.exists()})")
        print(f"events stored: {len(host.events(outcome.task_id))}")
        print(f"loaded plugins:  {list(host.plugins.names())}")
        print(f"process guards:  {host.guard_names()}")
        print(f"process observers: {host.observer_names()}")
        print(f"activated:       {list(host.options.plugins)}")
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
