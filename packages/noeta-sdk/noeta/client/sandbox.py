"""``SandboxExecEnvManager`` — the host-layer lifecycle for a sandbox backend (T5).

This is the seam that turns ``HostConfig.exec_env`` (pure *addressing* config,
D2) into a live :class:`~noeta.tools.fs.exec_env.ExecEnv` and owns its lifetime.
It is the "who provisions the container" layer the config deliberately does NOT
carry: the config is import-linter-safe for a backend to build, and the live
adapter is instantiated here — above ``noeta.tools`` (the SDK's ``noeta.client``
band can import the adapter; the tools band could never reach up to build it).

Placement (D3, revised by the T4 note): the spec first put provisioning in the
``noeta.agent.backend`` product layer. In practice the one call site that must
receive the live backend is ``SdkHost._build_engine`` → ``build_session_inputs``,
and ``SdkHost`` lives in ``noeta.client``; keeping the manager here lets the host
hold it directly (like ``_process_registry``) instead of threading yet another
injected callable down from the product. It stays import-linter-clean because
``noeta.client`` sits above ``noeta.tools``.

**v1 scope — one shared container per host (see the spec's D4 / non-goals).**
The spec's ideal is one sandbox per root-task *tree* (``key = session-root task
id``), provisioned *eagerly at host-bind* and addressed by a per-root
``exec_env_ref`` welded into ``TaskHostBoundPayload``. That per-root ref — and
the reconnect it enables — is **T6**; it is also the only point at which a
per-root key exists (the seed Engine that writes ``TaskCreated`` has no task id
yet, and it shares the Engine cache with the first driving turn, so a per-root
switch made only in ``_build_engine`` would be silently bypassed). Until then,
v1 addresses a single AIO container by its one ``base_url`` and routes that same
backend into *every* sandbox Engine — seed and drive alike — which trivially
satisfies "subtasks share the parent's container" (everything shares it) and
sidesteps the seed/drive cache collision. The cost, recorded as a v1
known-limitation, is that two concurrent sessions on one host share one container
working directory; per-root isolation arrives with T6's per-root provisioning.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from noeta.client.host_config import SandboxExecEnvConfig
from noeta.tools.fs.exec_env import AioSandboxExecEnv, ExecEnv


__all__ = ["SandboxExecEnvManager", "ExecEnvFactory"]


#: A factory that turns the addressing config into a live backend. Injected by
#: tests (a fake that opens no socket); production uses :func:`_default_factory`,
#: which reads the key from the environment at connect time (D5 — the secret
#: never rides in the config, a log, or an event).
ExecEnvFactory = Callable[[SandboxExecEnvConfig], ExecEnv]


def _default_factory(config: SandboxExecEnvConfig) -> ExecEnv:
    """Build the real AIO Sandbox adapter from the addressing config.

    The API key is resolved from the environment **here, at connect time**, so
    the addressing config stays free of the secret (D5). ``fence_token`` is left
    at its v1 placeholder (``None``): cross-generation fencing is a v2 concern
    (D1), and the seam already carries the field so v2 need not reshape it.
    """
    return AioSandboxExecEnv(
        base_url=config.base_url,
        api_key=config.resolve_api_key(),
    )


class SandboxExecEnvManager:
    """Owns a sandbox backend's lifetime for one host.

    Lazily builds a single shared :class:`ExecEnv` (v1: one container per host,
    keyed by nothing — see the module docstring) on first use and hands the same
    instance to every sandbox Engine build. Thread-safe: the SDK host resolves
    Engines under ``ThreadingHTTPServer`` concurrency, so the build is guarded by
    a double-checked lock (the adapter is otherwise a stateless HTTP client).

    ``teardown`` is the reap seam: on host shutdown the Client calls it so an
    idle container connection does not outlive the process. For a ``"eager"``
    host — the one that owns the container's lifetime — this is where a future
    container stop hooks in; for ``"attach"`` (a reconnect to a container someone
    else owns) it only drops the local handle and must never stop the container.
    Root-task-terminal teardown (D6) is T8; this method is what T8 wires to it.
    """

    def __init__(
        self,
        config: SandboxExecEnvConfig,
        *,
        factory: Optional[ExecEnvFactory] = None,
    ) -> None:
        self._config = config
        self._factory: ExecEnvFactory = factory or _default_factory
        self._lock = threading.Lock()
        self._exec_env: Optional[ExecEnv] = None

    @property
    def workdir(self) -> str:
        """The container working directory — the fs-tools' workspace root (D7)."""
        return self._config.workdir

    def exec_env(self) -> ExecEnv:
        """The host's shared sandbox backend, built on first use.

        Double-checked under the lock so a concurrent burst of Engine builds
        (delegated children, resident workers) provisions exactly once.
        """
        env = self._exec_env
        if env is not None:
            return env
        with self._lock:
            if self._exec_env is None:
                self._exec_env = self._factory(self._config)
            return self._exec_env

    def teardown(self) -> None:
        """Drop the shared backend, best-effort closing it first.

        Idempotent. ``"attach"`` never closes (the container belongs to whoever
        provisioned it — a stop here would break a still-running peer that
        reconnected to the same ``base_url``); ``"eager"`` best-effort closes if
        the adapter grows a ``close`` (v1's HTTP adapter holds no persistent
        resource, so this is a no-op today — the seam is for T8 / v2).
        """
        with self._lock:
            env = self._exec_env
            self._exec_env = None
        if env is None:
            return
        if self._config.provision == "eager":
            close = getattr(env, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Teardown must never raise from a shutdown path.
                    pass
