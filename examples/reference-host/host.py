"""Reference host — a minimal but real Noeta host built on ``noeta.sdk`` alone.

Demonstrated SDK capability
---------------------------
This is the split spec's **reference host** (``docs/implementation-specs/
2026-07-26-sdk-only-repo-split.md``, decision D5): the smallest self-contained
program that assembles a durable, plugin-extended, streaming Noeta agent using
**only the public surface** (``noeta.sdk`` / ``noeta.sdk.storage`` /
``noeta.presets``). It reaches no runtime internal — exactly the discipline the
split repo's import-linter will enforce on the real product — so it doubles as
the host-builder tutorial and the contract-test bed (``tests/test_reference_host.py``).

What it wires
-------------
* **Durable storage** — the sqlite triple (``SqliteEventLog`` /
  ``SqliteDispatcher`` / ``SqliteContentStore``) over one file, injected through
  :class:`~noeta.sdk.HostConfig`. Every event and content blob is persisted, so
  the session survives process death and folds back on reopen.
* **Token streaming** — a stdout delta sink wired as ``HostConfig.delta_sink``.
  When the injected provider is a ``StreamingProvider`` the runtime pushes each
  ephemeral :class:`~noeta.sdk.StreamDelta` through the sink while a turn is in
  flight (the deltas are previews only — never persisted).
* **Plugins** — the first-party example **manifest plugins** under
  ``examples/plugins/*`` are loaded by explicit path
  (:func:`~noeta.sdk.load_plugins` → a :class:`~noeta.sdk.PluginSet`) and
  handed to :class:`~noeta.sdk.Client` as ``plugins=``. The Client wires them
  per the effect-scoping rules (spec D6): the ``guard`` / ``observer`` plugins
  (``protected-paths`` / ``approval-modes`` / ``git-checkpoint``) are governance
  authority, in force process-wide once loaded; the ``redaction``
  ``tool_result_transform`` is per-agent, so the host **activates** it on the
  main agent (``Options.plugins``). Plugin config is *orthogonal to identity* in
  the manifest mechanism, so the host injects it through the **environment** (it
  points ``protected-paths`` at the session workspace).
* **A preset recipe** — the agent identity comes from
  :func:`noeta.presets.main_options` (the official ``main`` agent), so the host
  ships a real, capable agent rather than a toy prompt.

Swapping in a real provider
---------------------------
The host is **provider-agnostic**: :func:`build_reference_host` takes whatever
``provider`` you hand it. The offline demo (:func:`main`) hands it
:class:`_ScriptedStreamingProvider`, a network-free scripted streaming double
(the same shape as ``noeta.testing.fake_llm.FakeStreamingLLMProvider``, rebuilt
from the public message types). A production host swaps that one line for a real
provider — e.g. an ``AnthropicProvider`` or an ``OpenAICompatProvider`` pointed
at a gateway — and changes nothing else. See this directory's ``README.md``.

Run it::

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
    """A ``HostConfig.delta_sink`` that writes streamed token text to a stream.

    The runtime calls this ``(ctx, call_id, delta)`` for every ephemeral
    :class:`~noeta.sdk.StreamDelta` while a streaming provider call is in
    flight. We write the delta's text straight to ``out`` (default
    ``sys.stdout``) so a live token stream is visible, and count what we saw so
    a host (or a test) can confirm streaming actually happened. Deltas are
    previews — losing them is harmless — so the sink never raises back into the
    call: a broken consumer must not fail the turn.
    """

    def __init__(self, out: Optional[TextIO] = None) -> None:
        self._out: TextIO = out if out is not None else sys.stdout
        self.delta_count = 0

    def __call__(
        self, ctx: StepContext, call_id: str, delta: StreamDelta
    ) -> None:
        try:
            # Both delta kinds go to the same stream here. A real product routes
            # ``thinking`` (the model's private reasoning preview) to its own
            # pane instead of interleaving it with the answer text.
            self._out.write(delta.text)
            self._out.flush()
        except Exception:
            # Never let a slow / broken output stream fail the LLM call.
            return
        self.delta_count += 1


# ---------------------------------------------------------------------------
# Plugin discovery (explicit-path loading of examples/plugins/*)
# ---------------------------------------------------------------------------

#: The example plugins whose effect the reference host wires. The three
#: governance plugins are process-wide once loaded (no activation needed); the
#: three per-agent surfaces (``redaction``'s ``tool_result_transform``,
#: ``checklist-reminder``'s compose-time ``reminder``, ``memory-recall``'s
#: recorded ``reminder_provider``) are also named in :func:`default_activation`,
#: because a per-agent surface fires only for an agent that activates it (D6).
_WIRED_PLUGINS: tuple[str, ...] = (
    "protected-paths",
    "approval-modes",
    "git-checkpoint",
    "redaction",
    "checklist-reminder",
    "memory-recall",
)


def default_plugin_modules() -> list[str]:
    """The wired example plugins' ``plugin.py`` paths, sorted (reproducible order).

    A server host discovers plugins via entry points + an operator enable-list;
    in this repo the example plugins are not installed, so the reference host
    loads them by explicit file path — the ``modules=[...]`` seam of
    :func:`~noeta.sdk.load_plugins`. Load order never affects the compiled
    ``AgentSpec`` (the merge is deterministic), but we sort here anyway so the
    discovered set is stable.
    """
    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    return sorted(str(plugins_dir / name / "plugin.py") for name in _WIRED_PLUGINS)


def default_activation() -> tuple[str, ...]:
    """The loaded plugins the main agent **activates** (per-agent surfaces, D6).

    The three feature plugins — ``redaction`` (a ``tool_result_transform``),
    ``checklist-reminder`` (a compose-time ``reminder``) and ``memory-recall`` (a
    recorded ``reminder_provider``) — each fire only for an agent that activates
    them. The governance guards / observer apply process-wide without it.
    """
    return ("redaction", "checklist-reminder", "memory-recall")


def apply_plugin_env(workspace_dir: Path) -> dict[str, str]:
    """Inject the example plugins' config through the environment (D1).

    The manifest mechanism keeps config *orthogonal to agent identity* — it
    resolves a contribution's ``ref`` to a live object and never threads a
    per-plugin config dict — so a host configures a plugin the 12-factor way:
    through the environment, read when the plugin module is imported. Here the
    host points ``protected-paths`` and ``git-checkpoint`` at the session
    workspace. Returns the variables it set (for transparency / the demo).

    Must run **before** the plugin modules are loaded (they read the environment
    at import). Returns the injected mapping.

    This mutates ``os.environ`` for the rest of the process — right for a real
    host, which configures itself once at startup and then runs. A caller that
    builds hosts repeatedly in ONE process (a test suite, a multi-tenant driver)
    wants :func:`plugin_env_scope` instead: variables pointed at a per-build
    temporary directory outlive that directory if never restored, so the NEXT
    build reads a path that no longer exists.
    """
    env = _plugin_env(workspace_dir)
    os.environ.update(env)
    return env


@contextmanager
def plugin_env_scope(workspace_dir: Path) -> Iterator[dict[str, str]]:
    """:func:`apply_plugin_env` for the duration of a block, then restored.

    The plugin modules read their config at import, so the variables only have to
    be in force while :func:`~noeta.sdk.load_plugins` runs — after that the
    guards are built and the values have done their job. Restoring on exit is
    what keeps one build's workspace path out of the next one's environment.
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

    Built by :func:`build_reference_host`. Holds the sqlite triple (so it can be
    closed cleanly), the agent :class:`~noeta.sdk.Options` (identity + the
    per-agent activation), the loaded :class:`~noeta.sdk.PluginSet` (the host
    hands it to the Client, which wires guards / observers / transforms per D6),
    the wired :class:`~noeta.sdk.Client`, and the streaming sink. :meth:`run`
    drives one session turn.
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
        """The names of the process-wide guards the loaded plugins contribute."""
        guards, _observers = self.plugins.process_hooks()
        return sorted(getattr(g, "name", type(g).__name__) for g in guards)

    def observer_names(self) -> list[str]:
        """The names of the process-wide observers the loaded plugins contribute."""
        _guards, observers = self.plugins.process_hooks()
        return sorted(getattr(o, "name", type(o).__name__) for o in observers)

    def run(self, goal: str) -> Any:
        """Create the session (if new) and drive one turn to its trailing stop.

        Returns the driver outcome (``task_id`` / ``status`` / ``wake_handle``).
        With the multi-turn policy a finished turn suspends on the next-goal
        handle — a live session awaiting the next message — rather than closing
        the task; the streamed answer is already in the event log by then.
        """
        return self.client.start(goal=goal)

    def messages(self, task_id: str) -> list[Any]:
        """The human-readable view of ``task_id`` (``Client.messages``)."""
        return self.client.messages(task_id)

    def events(self, task_id: str) -> list[Any]:
        """The raw durable envelope stream for ``task_id`` (``Client.events``)."""
        return self.client.events(task_id)

    def close(self) -> None:
        """Shut the client down and close the sqlite connections.

        ``Client.shutdown`` unsubscribes observers and reaps runtime resources
        but deliberately does not close the injected stores (the host owns
        them), so the host closes the sqlite triple here.
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

    Parameters
    ----------
    provider:
        Any object satisfying ``LLMProvider`` (and, for token streaming, the
        optional ``StreamingProvider`` capability). Injected — the host is
        provider-neutral. A real host passes ``AnthropicProvider`` /
        ``OpenAICompatProvider``; the offline demo passes
        :class:`_ScriptedStreamingProvider`.
    workspace_dir:
        The session's working directory (fs / shell tools are fenced to it).
    db_path:
        The single sqlite file backing the storage triple. ``None`` (default)
        allocates a fresh file under a temp dir — the "tmp/dev path".
    plugin_modules:
        Explicit ``plugin.py`` paths to load. ``None`` ⇒
        :func:`default_plugin_modules` (the wired example plugins).
    activate:
        The loaded plugins the main agent activates (per-agent surfaces, D6).
        ``None`` ⇒ :func:`default_activation` (``redaction``). Governance
        guards / observers do not need activation.
    base_options:
        The base agent recipe. ``None`` ⇒ :func:`noeta.presets.main_options`
        (the official ``main`` agent).
    delta_out:
        Where the streaming sink writes token text. ``None`` ⇒ ``sys.stdout``.
    """
    if db_path is None:
        db_path = Path(tempfile.mkdtemp(prefix="noeta-reference-host-")) / "noeta.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. The durable storage triple over one sqlite file. Construct the three
    #    against the same path, dispatcher first (the event log takes it as its
    #    lease_validator) — the exact recipe noeta.sdk.storage documents.
    dispatcher = SqliteDispatcher(str(db_path))
    event_log = SqliteEventLog(str(db_path), lease_validator=dispatcher)
    content_store = SqliteContentStore(str(db_path))

    # 2. Inject plugin config through the environment BEFORE loading (the plugin
    #    modules read it at import), then discover + load the example manifest
    #    plugins by explicit path. A broken plugin fails loudly right here, at
    #    build time — never a mid-session turn. ``builtins=False`` keeps the
    #    loaded set to just the example plugins (noeta's own capabilities are
    #    already in the preset recipe).
    modules = (
        list(plugin_modules)
        if plugin_modules is not None
        else default_plugin_modules()
    )
    with plugin_env_scope(workspace_dir):
        plugins = load_plugins(builtins=False, modules=modules)

    # 3. The agent recipe + per-agent activation. The governance guards /
    #    observer apply process-wide once loaded; ``redaction`` is a per-agent
    #    ``tool_result_transform`` the main agent must activate (Options.plugins).
    base = base_options if base_options is not None else presets.main_options()
    activation = tuple(activate) if activate is not None else default_activation()
    new_activation = tuple(base.plugins) + tuple(
        name for name in activation if name not in base.plugins
    )
    options = replace(base, plugins=new_activation)

    # 4. Host-level wiring: the injected storage triple + the streaming sink.
    #    None of this touches the agent identity. Host-plane plugin contributions
    #    (mcp_server / skills) would be read via ``plugins.contributions(surface)``
    #    and wired into HostConfig; the example corpus contributes none.
    delta_sink = StdoutDeltaSink(delta_out)
    host_config = HostConfig(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        delta_sink=delta_sink,
    )

    # 5. The live client over the recipe + host config + loaded PluginSet. The
    #    Client resolves the plugins and wires them per D6. multi_turn keeps the
    #    session open after a finished turn (a real interactive host).
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

    Mirrors ``noeta.testing.fake_llm.FakeStreamingLLMProvider`` but is rebuilt
    from the public message types so the reference host stays a pure-public-surface
    program. It answers with one fixed end-of-turn message, streaming that
    message as a few text deltas first (the push-shaped contract real streaming
    adapters implement). A production host deletes this and injects a real
    provider instead.
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
        # Split the answer into a handful of streamed fragments (word-ish), so
        # the deltas concatenate back to the final content — the invariant a
        # real streaming adapter guarantees.
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
