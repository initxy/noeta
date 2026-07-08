"""``SandboxExecEnvManager`` — the SDK-side lifecycle over a ``SandboxProvider``.

This is the seam that turns a :class:`~noeta.client.sandbox_provider.SandboxProvider`
(the agent layer's "who provisions the container", D1) into live
:class:`~noeta.tools.fs.exec_env.ExecEnv` backends and owns their lifetime,
**keyed per session root** (v2, D4). It sits above ``noeta.tools`` (the SDK's
``noeta.client`` band may import the AIO adapter; the tools band could never
reach up to build it), so ``SdkHost`` holds it directly (like
``_process_registry``) instead of threading a callable down from the product.

**v1 → v2.** v1 addressed ONE external container by ``base_url`` and cached a
single backend keyed by that URL — every session on the host shared it. v2 makes
the container **per root-task tree**:

* :meth:`allocate` provisions a fresh container for a ``session_root_id``
  (eagerly, at ``driver.seed_start``) and returns the durable ``exec_env_ref``
  (``"{base_url}#{sandbox_id}"``) welded onto ``TaskHostBound``.
* :meth:`resolve` builds (and caches) the ``ExecEnv`` backend for a recorded
  ``exec_env_ref`` — the reconnect path: a handle allocated on THIS host is
  cached; a ref only seen on the durable record (resume / reclaim, possibly
  another host) is reconnected via ``provider.attach``.
* :meth:`release` tears one session's container down at its root-task terminal;
  :meth:`teardown` reaps everything left as a process-shutdown backstop.

**Attach-one-container back-compat.** The v1 ``HostConfig.exec_env``
(:class:`~noeta.client.host_config.SandboxExecEnvConfig`) deployment — a single
pre-existing container addressed by ``base_url`` — is preserved by
:class:`_ConfigAttachProvider`, a degenerate provider that *attaches* the one
configured container (``allocate`` == attach, ``release`` a no-op) and mints no
``sandbox_id`` (so the ref stays a bare ``base_url``, byte-identical to v1). The
manager itself has ONE code path (provider-based); only how the provider is
supplied differs.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Callable, Optional

from noeta.client.host_config import SandboxExecEnvConfig
from noeta.client.sandbox_provider import (
    MountSpec,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
    encode_exec_env_ref,
)
from noeta.tools.fs.exec_env import AioSandboxExecEnv, ExecEnv


__all__ = [
    "BackendFactory",
    "SandboxExecEnvManager",
    "provider_for_config",
]


#: Builds a live backend from an allocated / attached handle. Injected by tests
#: (a fake that opens no socket); production uses :func:`_default_backend_factory`.
#: The auth strategy is passed as a per-call header factory (D8) so a short-lived
#: credential is minted fresh each request; the addressing came off the handle.
BackendFactory = Callable[[SandboxHandle], ExecEnv]


def _default_backend_factory(handle: SandboxHandle) -> ExecEnv:
    """Build the real AIO adapter for a container handle.

    ``auth_headers`` wires the handle's live :class:`SandboxAuth` in as the
    adapter's **per-call** header factory (D8): the secret is fetched on the wire
    each request, never held on a durable object (D5). ``fence_token`` stays at
    its v1 placeholder (``None``) — cross-generation fencing is v2 orchestration
    (D7), and the seam already carries the field.
    """
    return AioSandboxExecEnv(
        base_url=handle.base_url,
        auth_headers=handle.auth.connect_headers,
    )


class _ConfigAttachProvider:
    """A degenerate :class:`SandboxProvider` that ATTACHES one existing container.

    Wraps the v1 :class:`SandboxExecEnvConfig`: it never provisions — ``allocate``
    just returns a handle for the single configured ``base_url`` and ``release``
    is a no-op (it does not own the container, so a stop here would break a peer
    that reconnected to the same address). ``sandbox_id`` is empty so the ref
    encodes to a bare ``base_url`` — byte-identical to a v1 recording. This keeps
    the "attach one shared container" deployment (and its gated e2e) working
    through the v2 provider seam with no product change.
    """

    __slots__ = ("_config",)

    def __init__(self, config: SandboxExecEnvConfig) -> None:
        self._config = config

    def _handle(self, base_url: str) -> SandboxHandle:
        return SandboxHandle(
            base_url=base_url,
            sandbox_id="",
            auth=StaticApiKeyAuth(self._config.api_key_env),
            workdir=self._config.workdir,
        )

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle:
        del session_root_id, spec  # attach ignores per-session provisioning
        return self._handle(self._config.base_url)

    def release(self, session_root_id: str) -> None:
        del session_root_id  # never owns the container

    def attach(self, exec_env_ref: str) -> SandboxHandle:
        # Reconnect to the RECORDED address (multi-machine criterion): a task
        # reclaimed on another host reads its own base_url off the ref, not this
        # host's config default. Falls back to the config default for an empty
        # ref. Credentials come from THIS host's env (D5), never the ref.
        base_url, _ = decode_exec_env_ref(exec_env_ref)
        return self._handle(base_url or self._config.base_url)


def provider_for_config(config: SandboxExecEnvConfig) -> SandboxProvider:
    """Adapt a v1 ``SandboxExecEnvConfig`` into the v2 ``SandboxProvider`` seam."""
    return _ConfigAttachProvider(config)


class SandboxExecEnvManager:
    """Owns a host's per-session sandbox backends over a ``SandboxProvider``.

    Backends are cached per ``exec_env_ref`` (a session's bound container); a
    handle allocated on this host is remembered so a same-host resolve reuses it,
    and a ref seen only on the durable record is reconnected via
    ``provider.attach``. Thread-safe: the SDK host resolves Engines under
    ``ThreadingHTTPServer`` concurrency, so the cache map is lock-guarded (each
    adapter is otherwise a stateless HTTP client).

    ``spec_template`` carries the deployment-fixed half of a
    :class:`SandboxSpec` (image, resource caps, the built-in / global skills
    mounts); :meth:`allocate` combines it with the per-session workspace mount.
    ``default_workdir`` is the container workspace-mount target (and the fs
    tools' lexical root) — ``/workspace`` for the provisioning path, the
    config's ``workdir`` for attach.
    """

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        spec_template: SandboxSpec,
        default_workdir: str = "/workspace",
        default_ref: Optional[str] = None,
        backend_factory: Optional[BackendFactory] = None,
    ) -> None:
        self._provider = provider
        self._spec_template = spec_template
        self._default_workdir = default_workdir
        #: The container a build with NO session-welded ref falls back to — the
        #: attach path's single shared container (v1 back-compat). ``None`` on the
        #: per-session provisioning path, where every real build carries the ref
        #: the driver allocated and there is no "default container".
        self.default_ref = default_ref
        self._factory: BackendFactory = backend_factory or _default_backend_factory
        self._lock = threading.Lock()
        self._handles_by_root: dict[str, SandboxHandle] = {}
        self._handles_by_ref: dict[str, SandboxHandle] = {}
        self._refs_by_root: dict[str, str] = {}
        self._backends_by_ref: dict[str, ExecEnv] = {}

    # -- provisioning (D4) ------------------------------------------------- #

    def allocate(
        self, session_root_id: str, *, host_workspace: Optional[str] = None
    ) -> str:
        """Provision a fresh container for ``session_root_id`` → its durable ref.

        Assembles the per-session :class:`SandboxSpec` (the template's fixed
        mounts + the session's workspace mount at ``default_workdir``), calls
        ``provider.allocate``, caches the returned handle by both root id and
        ref, and returns the ``exec_env_ref`` the driver welds onto
        ``TaskHostBound`` (``"{base_url}#{sandbox_id}"``, or a bare ``base_url``
        when the provider mints no id).
        """
        mounts = list(self._spec_template.mounts)
        if host_workspace:
            mounts.append(
                MountSpec(
                    source=host_workspace,
                    target=self._default_workdir,
                    mode="rw",
                    kind="local-path",
                )
            )
        spec = dataclasses.replace(self._spec_template, mounts=tuple(mounts))
        handle = self._provider.allocate(session_root_id, spec)
        ref = encode_exec_env_ref(handle.base_url, handle.sandbox_id)
        with self._lock:
            self._handles_by_root[session_root_id] = handle
            self._handles_by_ref[ref] = handle
            self._refs_by_root[session_root_id] = ref
        return ref

    # -- backend resolution (build + reconnect) ---------------------------- #

    def resolve(self, exec_env_ref: str) -> tuple[ExecEnv, str]:
        """The ``(backend, container workdir)`` for a bound ``exec_env_ref``.

        Built on first request per ref and cached. A ref whose handle this host
        allocated is reused directly; a ref seen only on the durable record
        (resume / reclaim, possibly another host) is reconnected via
        ``provider.attach`` — the reconnect path. The API key always comes from
        this host's env (D5), never the ref.
        """
        with self._lock:
            backend = self._backends_by_ref.get(exec_env_ref)
            if backend is not None:
                return backend, self._handles_by_ref[exec_env_ref].workdir
        # Slow path: obtain the handle (cached from a local allocate, or attach)
        # and build the backend. Not holding the lock across attach's readiness
        # probe; a concurrent race just builds twice and the last write wins the
        # cache (each adapter is a stateless HTTP client).
        handle = self._handles_by_ref.get(exec_env_ref)
        if handle is None:
            handle = self._provider.attach(exec_env_ref)
        backend = self._factory(handle)
        with self._lock:
            self._handles_by_ref[exec_env_ref] = handle
            self._backends_by_ref[exec_env_ref] = backend
        return backend, handle.workdir

    # -- lifecycle (D4) ---------------------------------------------------- #

    def release(self, session_root_id: str) -> None:
        """Tear down one session's container (idempotent — unknown id is a no-op).

        Drops the cached handle / backend for that root and calls
        ``provider.release``. Called at the session's root-task terminal (and via
        :meth:`teardown` on shutdown). Releasing a session that never allocated
        is a clean no-op (the local / non-sandbox path never reaches here)."""
        with self._lock:
            ref = self._refs_by_root.pop(session_root_id, None)
            self._handles_by_root.pop(session_root_id, None)
            # Evict the cached backend/handle only for a per-session ref this
            # root uniquely owns. The attach path's shared ``default_ref`` is
            # referenced by EVERY peer session, so dropping it here would force
            # each peer to rebuild on its next resolve — harmless (a stateless
            # HTTP client) but pointless cache churn. ``default_ref`` is None on
            # the per-session provisioning path, where every ref is unique, so
            # this guard never withholds an eviction there.
            if ref is not None and ref != self.default_ref:
                self._handles_by_ref.pop(ref, None)
                self._backends_by_ref.pop(ref, None)
        self._provider.release(session_root_id)

    def teardown(self) -> None:
        """Release every still-open session container. Idempotent shutdown reap.

        A backstop for sessions that never reached a root terminal (an
        interactive conversation resting at ``suspended`` when the process exits)
        so no container outlives the host. Never raises from a shutdown path."""
        with self._lock:
            roots = list(self._handles_by_root)
            self._handles_by_root.clear()
            self._handles_by_ref.clear()
            self._refs_by_root.clear()
            self._backends_by_ref.clear()
        for root in roots:
            try:
                self._provider.release(root)
            except Exception:
                # Teardown must never raise from a shutdown path.
                pass
