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
* **Plugins** — the first-party example plugins under ``examples/plugins/*`` are
  loaded by explicit path (:func:`~noeta.sdk.load_plugins`) and folded
  deterministically into the base ``Options`` (:func:`~noeta.sdk.merge_plugins`).
  Host-plane contributions (MCP servers / skill dirs) are read separately and
  wired into ``HostConfig``.
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

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, TextIO

from noeta.sdk import (
    Client,
    HostConfig,
    LLMRequest,
    LLMResponse,
    Options,
    StepContext,
    StreamDelta,
    TextBlock,
    Usage,
    load_plugins,
    merge_plugins,
    merged_mcp_servers,
    merged_skill_dirs,
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


def default_plugin_modules() -> list[str]:
    """Every ``examples/plugins/*/plugin.py`` path, sorted (for reproducibility).

    A server host discovers plugins via entry points + an operator enable-list;
    in this repo the example plugins are not installed, so the reference host
    loads them by explicit file path — the ``modules=[...]`` seam of
    :func:`~noeta.sdk.load_plugins`. Load order never affects the compiled
    ``AgentSpec`` (``merge_plugins`` sorts deterministically), but we sort here
    anyway so the discovered set is stable.
    """
    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    return sorted(str(p) for p in plugins_dir.glob("*/plugin.py"))


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------


@dataclass
class ReferenceHost:
    """A live, durable, plugin-extended Noeta session driver.

    Built by :func:`build_reference_host`. Holds the sqlite triple (so it can be
    closed cleanly), the merged :class:`~noeta.sdk.Options` (agent identity +
    the plugins' guards / observers), the wired :class:`~noeta.sdk.Client`, and
    the streaming sink. :meth:`run` drives one session turn.
    """

    client: Client
    options: Options
    db_path: Path
    delta_sink: StdoutDeltaSink
    _event_log: SqliteEventLog
    _content_store: SqliteContentStore
    _dispatcher: SqliteDispatcher

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
    plugin_config: Optional[Mapping[str, dict]] = None,
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
        :func:`default_plugin_modules` (the ``examples/plugins/*`` corpus).
    plugin_config:
        Per-plugin config dict (``{plugin_name: {...}}``) threaded to each
        plugin factory that declares a config parameter.
    base_options:
        The base agent recipe the plugins fold into. ``None`` ⇒
        :func:`noeta.presets.main_options` (the official ``main`` agent).
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

    # 2. Discover + load the example plugins by explicit path, then fold their
    #    typed contributions deterministically into the preset recipe. A
    #    collision (or a broken plugin) fails loudly right here, at build time —
    #    never a mid-session turn.
    modules = (
        list(plugin_modules)
        if plugin_modules is not None
        else default_plugin_modules()
    )
    loaded = load_plugins(modules=modules, config=plugin_config)
    base = base_options if base_options is not None else presets.main_options()
    options = merge_plugins(base, loaded)

    # 2b. Host-plane contributions live off Options (they have no identity
    #     surface): a host reads them separately and wires them into HostConfig.
    #     The example corpus contributes none, so these are empty — shown here as
    #     the pattern a richer host follows.
    mcp_servers = merged_mcp_servers(loaded)
    skill_dirs = merged_skill_dirs(loaded)  # noqa: F841 — wire where a host has a skills root
    mcp_resolver = (lambda alias: mcp_servers.get(alias)) if mcp_servers else None

    # 3. Host-level wiring: the injected storage triple + the streaming sink +
    #    any host-plane MCP resolver. None of this touches the agent identity.
    delta_sink = StdoutDeltaSink(delta_out)
    host_config = HostConfig(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        delta_sink=delta_sink,
        mcp_server_resolver=mcp_resolver,
    )

    # 4. The live client over the merged recipe + host config. multi_turn keeps
    #    the session open after a finished turn (a real interactive host).
    client = Client(
        options,
        provider=provider,
        workspace_dir=workspace_dir,
        host_config=host_config,
    )

    return ReferenceHost(
        client=client,
        options=options,
        db_path=db_path,
        delta_sink=delta_sink,
        _event_log=event_log,
        _content_store=content_store,
        _dispatcher=dispatcher,
    )


def default_plugin_config(workspace_dir: Path) -> dict[str, dict]:
    """A sensible per-plugin config for the example corpus, keyed by plugin name.

    * ``protected-paths`` — fence writes to the workspace (the guard refuses to
      load with neither an allowed root nor a deny glob, so this is required).
    * ``approval-modes`` — ``smart_approve``: allow read-only tools, ask for the
      rest.
    * ``git-checkpoint`` — snapshot the workspace repo around mutating tools.
    """
    return {
        "protected-paths": {"allowed_roots": [str(workspace_dir)]},
        "approval-modes": {"mode": "smart_approve"},
        "git-checkpoint": {"repo_path": str(workspace_dir)},
    }


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
        plugin_config=default_plugin_config(workspace),
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
        guard_names = sorted(getattr(g, "name", "?") for g in host.options.guards)
        print(f"active guards: {guard_names}")
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
