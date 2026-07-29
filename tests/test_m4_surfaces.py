"""M4 new surfaces (spec D9 + D10): ``tool_result_transform``, ``policy``,
``prompt_fragment`` ordering, ``sandbox_provider``.

Each surface is exercised for the three properties the milestone calls out —
collision, ordering, and activation scoping — plus the surface-specific
guarantee (D9: the transformed result is the ONLY thing recorded; D10: policy is
single-valued; sandbox_provider is host-plane and never follows activation).
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path
from typing import Any

import pytest

from noeta.agent.spec import ComponentRef
from noeta.client.options import (
    DEFAULT_PLUGINS,
    AgentDefinition,
    Options,
    PluginActivation,
    compile_options,
    effective_root_policy,
)
from noeta.client.parts import POLICY_REF
from noeta.client.plugin_set import load_plugins
from noeta.client.sandbox import SandboxExecEnvManager
from noeta.client.sandbox_provider import SandboxProvider, SandboxSpec
from noeta.protocols.decisions import ToolCall
from noeta.protocols.events import TaskHostBoundPayload
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import ToolRuntime
from noeta.sdk import HostConfig
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def _bare(**kw: object) -> Options:
    return Options(system_prompt="You are a helpful assistant.", **kw)  # type: ignore[arg-type]


# ===========================================================================
# D9 — tool_result_transform: a ToolRuntime pipeline stage, applied BEFORE
# recording so the transformed result is the only durable trace.
# ===========================================================================


_SECRET = "sk-TOP-SECRET-abc123"


class _LeakyTool:
    name = "leak"
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG002
        return ToolResult(
            success=True,
            output={"token": _SECRET},
            summary=f"fetched token {_SECRET}",
        )


def _redact(result: ToolResult) -> ToolResult:
    """A pure redaction stage — scrubs the secret from output + summary."""
    return dataclasses.replace(
        result,
        output={"token": "***"},
        summary=result.summary.replace(_SECRET, "***"),
    )


def test_transform_redaction_leaves_no_secret_in_ledger_or_content_store() -> None:
    """Acceptance 10: the transformed ToolResult is recorded; the secret reaches
    neither the ledger (ToolResultRecorded) nor the ContentStore blob."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    rt = ToolRuntime(
        event_log=log, content_store=store, tool_result_transforms=(_redact,)
    )
    call = ToolCall(tool_name="leak", arguments={}, call_id="c-1")

    result = rt.invoke(_LeakyTool(), call, task_id="t1", lease_id="l1", trace_id="tr1")

    # Returned + recorded result are the redacted ones.
    assert _SECRET not in result.summary
    recorded = log.read("t1")[1]
    assert recorded.type == "ToolResultRecorded"
    assert _SECRET not in recorded.payload.summary
    # The ContentStore blob for output_ref carries no secret either.
    body = store.get(recorded.payload.output_ref)
    assert body is not None and _SECRET.encode() not in body
    # And the whole event stream is secret-free.
    for env in log.read("t1"):
        assert _SECRET not in str(env.payload)


def test_transforms_apply_in_priority_order() -> None:
    """Stages run in the order the loader hands them (priority, plugin, name)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()

    def add_a(r: ToolResult) -> ToolResult:
        return dataclasses.replace(r, summary=r.summary + "A")

    def add_b(r: ToolResult) -> ToolResult:
        return dataclasses.replace(r, summary=r.summary + "B")

    rt = ToolRuntime(
        event_log=log, content_store=store, tool_result_transforms=(add_a, add_b)
    )

    class _Echo:
        name = "echo"
        risk_level = "low"
        input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

        def invoke(self, a: dict[str, Any], c: ToolContext) -> ToolResult:  # noqa: ARG002
            return ToolResult(success=True, summary="")

    result = rt.invoke(
        _Echo(), ToolCall(tool_name="echo", arguments={}, call_id="c"),
        task_id="t", lease_id="l", trace_id="tr",
    )
    assert result.summary == "AB"


def _boom(result: ToolResult) -> ToolResult:  # noqa: ARG001
    raise RuntimeError("transform exploded")


def _wrong_type(result: ToolResult) -> ToolResult:  # noqa: ARG001
    return "nope"  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("stage", "expected"),
    [(_boom, "raised"), (_wrong_type, "returned str")],
)
def test_a_broken_transform_fails_the_call_without_stranding_the_ledger(
    stage: Any, expected: str
) -> None:
    """A transform fault is contained inside the recording envelope.

    ``ToolCallStarted`` is already committed by the time a stage runs, so a stage
    that raises (or returns the wrong type) must NOT propagate: that would leave
    the assistant ``tool_use`` with no matching ``tool_result`` and the next
    compose→decide would take a fatal provider 400. Instead the call collapses to
    a failed result naming the stage — and, because a broken stage may be the
    redaction one, the untransformed payload is discarded rather than recorded.
    """
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    rt = ToolRuntime(
        event_log=log, content_store=store, tool_result_transforms=(stage,)
    )
    call = ToolCall(tool_name="leak", arguments={}, call_id="c-1")

    result = rt.invoke(_LeakyTool(), call, task_id="t1", lease_id="l1", trace_id="tr1")

    assert result.success is False
    assert expected in result.summary
    # The full three-event envelope recorded — no stranded tool_use.
    assert [e.type for e in log.read("t1")] == [
        "ToolCallStarted",
        "ToolResultRecorded",
        "ToolCallFinished",
    ]
    # The secret the broken stage was meant to scrub is nowhere durable.
    for env in log.read("t1"):
        assert _SECRET not in str(env.payload)
    body = store.get(log.read("t1")[1].payload.output_ref)
    assert body is not None and _SECRET.encode() not in body


# --- activation scoping: the transform follows the agent that activates it ---


_TRANSFORM_PLUGIN = textwrap.dedent(
    """
    noeta_plugin_name = "redactor"
    import dataclasses
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("redactor")

    @plugin.tool_result_transform(name="scrub", priority=50)
    def scrub(result):
        return dataclasses.replace(result, summary="[scrubbed]")
    """
)


def test_activation_transforms_are_keyed_by_plugin(tmp_path: Path) -> None:
    plugin_file = tmp_path / "redactor.py"
    plugin_file.write_text(_TRANSFORM_PLUGIN, encoding="utf-8")

    pset = load_plugins(builtins=False, modules=(str(plugin_file),))
    tmap = pset.activation_transforms()
    assert set(tmap) == {"redactor"}
    (prio, name, fn) = tmap["redactor"][0]
    assert (prio, name) == (50, "scrub")
    assert callable(fn)


# ===========================================================================
# D10 — policy: single-valued per agent.
# ===========================================================================


class _FakePolicyFactory:
    """A ``(llm) -> Policy`` factory carrying its identity ``.ref`` (D10)."""

    def __init__(self, ref_name: str) -> None:
        self._ref = ComponentRef(ref_name, "1")

    @property
    def ref(self) -> ComponentRef:
        return self._ref

    def __call__(self, llm: Any) -> Any:  # pragma: no cover — identity only
        return object()


def test_plugin_policy_sets_the_identity_ref() -> None:
    factory = _FakePolicyFactory("custom-brain")
    activation = {"brain": PluginActivation(policy=factory)}
    opts = _bare(plugins=DEFAULT_PLUGINS + ("brain",))
    main, _ = compile_options(opts, plugins=activation)
    assert main.policy == ComponentRef("custom-brain", "1")
    # The runtime half resolves the SAME factory for policy_override.
    assert effective_root_policy(opts, activation) is factory


def test_base_plus_plugin_policy_is_a_loud_collision() -> None:
    factory = _FakePolicyFactory("plugin-brain")
    activation = {"brain": PluginActivation(policy=factory)}
    opts = _bare(policy=_FakePolicyFactory("base-brain"), plugins=DEFAULT_PLUGINS + ("brain",))
    with pytest.raises(ValueError, match="policy is single-valued"):
        compile_options(opts, plugins=activation)


def test_two_plugin_policies_collide_naming_both() -> None:
    activation = {
        "a": PluginActivation(policy=_FakePolicyFactory("a")),
        "b": PluginActivation(policy=_FakePolicyFactory("b")),
    }
    opts = _bare(plugins=DEFAULT_PLUGINS + ("a", "b"))
    with pytest.raises(ValueError, match="active plugin 'a'.*active plugin 'b'"):
        compile_options(opts, plugins=activation)


def test_no_policy_keeps_the_react_default() -> None:
    main, _ = compile_options(_bare())
    assert main.policy == POLICY_REF
    assert effective_root_policy(_bare(), None) is None


def test_policy_follows_per_agent_activation() -> None:
    """A child activating one policy plugin is fine; two collide naming the child."""
    activation = {
        "a": PluginActivation(policy=_FakePolicyFactory("a")),
        "b": PluginActivation(policy=_FakePolicyFactory("b")),
    }
    ok = _bare(
        agents={"c": AgentDefinition(description="c", prompt="p", plugins=("a",))}
    )
    compile_options(ok, plugins=activation)  # single child policy: no error

    clash = _bare(
        agents={"c": AgentDefinition(description="c", prompt="p", plugins=("a", "b"))}
    )
    with pytest.raises(ValueError, match="AgentDefinition 'c'"):
        compile_options(clash, plugins=activation)


# ===========================================================================
# D10 — prompt_fragment: appended after the prompt, sorted (plugin, name).
# ===========================================================================


def test_prompt_fragments_order_by_plugin_then_name() -> None:
    activation = {
        "zeta": PluginActivation(prompt_fragments=(("a-frag", "ZETA"),)),
        "alpha": PluginActivation(prompt_fragments=(("z-frag", "ALPHA"),)),
    }
    opts = _bare(plugins=DEFAULT_PLUGINS + ("zeta", "alpha"))
    main, _ = compile_options(opts, plugins=activation)
    # Sorted by (plugin, name): 'alpha' precedes 'zeta' regardless of the
    # contribution names or activation order.
    assert main.instructions.endswith("ALPHA\n\nZETA")


# ===========================================================================
# D10 — sandbox_provider: host-plane; never follows per-agent activation.
# ===========================================================================


_SANDBOX_PLUGIN = textwrap.dedent(
    """
    noeta_plugin_name = "box"
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("box")

    class _Provider:
        name = "box"

    @plugin.sandbox_provider(name="box")
    def provider():
        return _Provider()
    """
)


def test_sandbox_provider_lists_without_execution_and_never_activates(
    tmp_path: Path,
) -> None:
    plugin_file = tmp_path / "box.py"
    plugin_file.write_text(_SANDBOX_PLUGIN, encoding="utf-8")

    pset = load_plugins(builtins=False, modules=(str(plugin_file),))
    # Listed by the zero-execution surface (contributions()).
    listed = pset.contributions("sandbox_provider")
    assert [(p, c.name) for p, c in listed] == [("box", "box")]
    # Host-plane: it is NOT an identity contribution and NOT a per-agent
    # transform — activating agents never pull it in.
    assert pset.identity_activations()["box"].tools == ()
    assert pset.activation_transforms() == {}


def test_builtin_sandbox_plugin_declares_the_aio_adapters() -> None:
    """The retirement-slated AIO adapters ride the built-in ``sandbox`` plugin."""
    pset = load_plugins(builtins=True)
    assert "sandbox" in pset.names()
    names = {c.name for _p, c in pset.contributions("sandbox_provider")}
    assert names == {"aio-exec-env", "aio-browser"}
    # The refs point at the AIO adapter classes (listing surface — no execution).
    refs = {c.ref for _p, c in pset.contributions("sandbox_provider")}
    assert refs == {
        "noeta.runtime.exec_env:AioSandboxExecEnv",
        "noeta.tools.browser:AioBrowserBackend",
    }


# --- AC11 as a single flow: resolve out of the PluginSet, wire onto the host,
#     allocate + reattach across resume, and prove the key is never durable. ---


_SANDBOX_SECRET = "sk-SANDBOX-live-only-9f"

_SANDBOX_PROVIDER_PLUGIN = textwrap.dedent(
    """
    noeta_plugin_name = "box"
    from noeta.sdk import PluginBuilder
    from noeta.client.sandbox_provider import (
        SandboxHandle,
        StaticApiKeyAuth,
        decode_exec_env_ref,
    )

    plugin = PluginBuilder("box")

    class _BoxProvider:
        # A real SandboxProvider: a fresh handle per allocate; attach reconnects
        # by the recorded ADDRESS. The api key rides only on the live
        # StaticApiKeyAuth (read from env at connect time), never on the handle.
        def __init__(self):
            self._n = 0

        def allocate(self, session_root_id, spec):
            self._n += 1
            return SandboxHandle(
                base_url=f"http://box-{self._n}:8080",
                sandbox_id=f"sid-{self._n}",
                auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
                workdir="/workspace",
            )

        def release(self, session_root_id):
            pass

        def attach(self, exec_env_ref):
            base_url, sandbox_id = decode_exec_env_ref(exec_env_ref)
            return SandboxHandle(
                base_url=base_url,
                sandbox_id=sandbox_id,
                auth=StaticApiKeyAuth("SANDBOX_API_KEY"),
                workdir="/workspace",
            )

    @plugin.sandbox_provider(name="box")
    def provider():
        return _BoxProvider()
    """
)


class _AddrBackend:
    """A backend stub tagged with the base_url its handle addressed (no socket)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


def _addr_factory() -> Any:
    return lambda handle, preamble=None: _AddrBackend(handle.base_url)


def test_sandbox_provider_end_to_end_from_plugin_surface_to_reattach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 11 as a SINGLE flow through the plugin surface: a host resolves
    a ``sandbox_provider`` out of a loaded ``PluginSet``, wires it onto
    ``HostConfig.sandbox_provider`` (host wiring — never identity), and drives
    allocate + reattach-across-resume through it. The dedicated leak-scan proves
    the api key rides only on the live ``SandboxAuth`` — never the durable ref
    nor the ledger (D10)."""
    monkeypatch.setenv("SANDBOX_API_KEY", _SANDBOX_SECRET)
    plugin_file = tmp_path / "box.py"
    plugin_file.write_text(_SANDBOX_PROVIDER_PLUGIN, encoding="utf-8")

    # 1. Resolve the provider OUT of the loaded set — the one step that executes
    #    plugin code (contributions() listed it earlier without execution).
    pset = load_plugins(builtins=False, modules=(str(plugin_file),))
    resolved = pset.get("box").resolve(pset.registry)
    factory = next(r.value for r in resolved if r.surface == "sandbox_provider")
    provider = factory()
    assert isinstance(provider, SandboxProvider)

    # 2. Host selection: the provider goes onto HostConfig, never onto identity.
    hc = HostConfig(sandbox_provider=provider)
    assert hc.sandbox_provider is provider

    # 3. Drive allocate through the SDK's consumer of that wiring (host._sandbox).
    mgr = SandboxExecEnvManager(
        hc.sandbox_provider,
        spec_template=SandboxSpec(image="img"),
        backend_factory=_addr_factory(),
    )
    ref = mgr.allocate("task-root", host_workspace=str(tmp_path))
    # Record the ref durably exactly as the Engine welds it onto TaskHostBound.
    log = InMemoryEventLog()
    log.system_emit(
        task_id="task-root",
        type="TaskHostBound",
        payload=TaskHostBoundPayload(host_id="h1", exec_env_ref=ref),
        actor="engine",
        origin="engine",
    )

    # 4. Resume / reclaim on a FRESH manager (possibly another host): reconnect by
    #    the recorded ref via provider.attach — hitting the SAME container.
    resumed = SandboxExecEnvManager(
        provider,
        spec_template=SandboxSpec(image="img"),
        backend_factory=_addr_factory(),
    )
    reattached, _ = resumed.resolve(ref)
    assert reattached.base_url == mgr.resolve(ref)[0].base_url

    # 5. Leak-scan: the secret is reachable on the LIVE auth, but nowhere durable
    #    — not in the exec_env_ref, not in any recorded ledger envelope.
    assert provider.attach(ref).auth.connect_headers() == {
        "X-AIO-API-Key": _SANDBOX_SECRET
    }
    assert _SANDBOX_SECRET not in ref
    for env in log.read("task-root"):
        assert _SANDBOX_SECRET not in str(env.payload)
