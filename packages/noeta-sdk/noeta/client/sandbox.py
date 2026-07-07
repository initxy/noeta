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

**v1 scope — one container per host, addressed by ``base_url`` (D4 / non-goals).**
The spec's ideal is one sandbox per root-task *tree* (``key = session-root task
id``), each a distinct container. v1 does not orchestrate containers (a
non-goal): a ``SandboxExecEnvConfig`` names ONE external container by its
``base_url``, so a host addresses its container by that URL and every session on
the host shares it (which trivially satisfies "subtasks share the parent's
container"). The **T6** durable ``exec_env_ref`` welded into ``TaskHostBound``
records that base_url per session so a resumed / reclaimed session — possibly on
another host whose config default differs — reconnects to the SAME container by
its recorded address. The manager therefore keys backends by resolved base_url:
normally just the host default; a reconnect may add the recorded ref's address.
The v1 simplification (a known-limitation) is that two concurrent sessions on
one host share one container working directory, and the ``exec_env_ref`` carries
only the base_url, not the spec's ``{base_url, sandbox_id}`` — a distinct
``sandbox_id`` and per-root isolation arrive with v2 per-container orchestration.
"""

from __future__ import annotations

import dataclasses
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
    """Owns a host's sandbox backends, keyed by container ``base_url``.

    v1 is one container per host (the config's ``base_url``), but T6's reconnect
    means the manager may also be asked for a container at a DIFFERENT address:
    a task reclaimed on this host records its own ``exec_env_ref`` (the base_url
    it was originally bound to), which may not equal this host's config default
    if the deployment moved. So backends are cached per resolved base_url; every
    one is built with THIS host's API key (from config env, D5), never a key off
    the ref. Thread-safe: the SDK host resolves Engines under
    ``ThreadingHTTPServer`` concurrency, so builds are guarded by a lock (each
    adapter is otherwise a stateless HTTP client).

    ``current_ref`` is what a NEW session welds into ``TaskHostBound`` — the
    host's configured container address, made durable so a later reconnect (T6)
    reaches the same container. ``teardown`` is the reap seam: on host shutdown
    the Client calls it so idle container connections do not outlive the process.
    An ``"eager"`` host owns the container lifetime (a future container stop
    hooks in here); an ``"attach"`` host only drops local handles and must never
    stop a container someone else owns. Root-task-terminal teardown (D6) is T8.
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
        self._by_url: dict[str, ExecEnv] = {}

    @property
    def workdir(self) -> str:
        """The container working directory — the fs-tools' workspace root (D7)."""
        return self._config.workdir

    def current_ref(self) -> str:
        """The ``exec_env_ref`` a new session welds — the host's container URL.

        Made durable on ``TaskHostBound`` so a resumed / reclaimed session (T6),
        possibly on another host, reconnects to THIS container by address rather
        than the folding host's own config. Addressing only — never the key (D5).
        """
        return self._config.base_url

    def exec_env(self, *, base_url: Optional[str] = None) -> ExecEnv:
        """A sandbox backend for ``base_url`` (the host default when ``None``).

        Built on first request per resolved address and cached; a concurrent
        burst of Engine builds (delegated children, resident workers) provisions
        each address exactly once. ``base_url`` is a recorded ``exec_env_ref``
        the reconnect path passes; the API key always comes from this host's
        config env (D5), so the built adapter targets the ref's address with the
        host's credentials.
        """
        target = base_url or self._config.base_url
        env = self._by_url.get(target)
        if env is not None:
            return env
        with self._lock:
            env = self._by_url.get(target)
            if env is None:
                cfg = (
                    self._config
                    if target == self._config.base_url
                    else dataclasses.replace(self._config, base_url=target)
                )
                env = self._factory(cfg)
                self._by_url[target] = env
            return env

    def teardown(self) -> None:
        """Drop every cached backend, best-effort closing each first.

        Idempotent. ``"attach"`` never closes (a container belongs to whoever
        provisioned it — a stop here would break a still-running peer that
        reconnected to the same ``base_url``); ``"eager"`` best-effort closes if
        the adapter grows a ``close`` (v1's HTTP adapter holds no persistent
        resource, so this is a no-op today — the seam is for T8 / v2).
        """
        with self._lock:
            envs = list(self._by_url.values())
            self._by_url.clear()
        if self._config.provision != "eager":
            return
        for env in envs:
            close = getattr(env, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Teardown must never raise from a shutdown path.
                    pass
