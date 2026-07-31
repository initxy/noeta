"""``SandboxProvider`` — the per-session container provisioning seam.

The sandbox is scoped **per root-task tree**: a fresh container is provisioned
when a session opens and torn down when it ends. This module defines the seam the
SDK consumes and the host layer implements — the SDK never shells out to
``docker`` or a K8s API itself.

* :class:`SandboxProvider` — ``allocate`` / ``release`` / ``attach``, the one
  interface the SDK's :class:`~noeta.client.sandbox.SandboxExecEnvManager` drives.
* :class:`SandboxHandle` — what ``allocate`` returns: the durable-safe
  *addressing* half (``base_url`` / ``sandbox_id`` / ``workdir``) plus a live
  :class:`SandboxAuth` strategy that is **never** serialized.
* :class:`SandboxSpec` / :class:`MountSpec` — the ``allocate`` input: image,
  resource caps, and the mount list. ``MountSpec.kind`` abstracts the storage
  backend so a local ``docker -v`` and a distributed NAS mount are the same shape.
* :class:`SandboxAuth` / :class:`StaticApiKeyAuth` — auth is a **strategy**, not a
  static key, so a Bearer-JWT provider drops in with no seam change.

A session's bound container is recorded on ``TaskHostBound.exec_env_ref`` so a
resumed / reclaimed session reconnects to the SAME container. The ref stays a
flat ``str``: ``base_url`` and ``sandbox_id`` are packed as
``"{base_url}#{sandbox_id}"`` and split on the LAST ``#`` (a URL carries no bare
``#`` except a fragment delimiter, which these base URLs never use). An empty
``sandbox_id`` encodes to the bare ``base_url``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


__all__ = [
    "MountSpec",
    "SandboxAuth",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxSpec",
    "StaticApiKeyAuth",
    "decode_exec_env_ref",
    "encode_exec_env_ref",
]


# --------------------------------------------------------------------------- #
# auth strategy
# --------------------------------------------------------------------------- #


@runtime_checkable
class SandboxAuth(Protocol):
    """How a session authenticates to its container — a **strategy**, not a key.

    :meth:`connect_headers` is called **per HTTP request**, so a short-lived
    credential is minted fresh each call. The value rides only on the wire —
    never recorded, logged, or serialized. Because it is a live object, a
    :class:`SandboxHandle` does not serialize it; a reconnecting host rebuilds the
    strategy from its own local config.
    """

    def connect_headers(self) -> dict[str, str]: ...


class StaticApiKeyAuth:
    """Auth strategy backed by a static ``SANDBOX_API_KEY`` from the environment.

    The key is read from ``env_name`` **at connect time** — never at construction,
    never held on a durable object — so the secret is fetched only on the wire. An
    unset env var yields no header (an unauthenticated container).
    """

    __slots__ = ("_env_name",)

    def __init__(self, env_name: str = "SANDBOX_API_KEY") -> None:
        self._env_name = env_name

    def connect_headers(self) -> dict[str, str]:
        key = os.environ.get(self._env_name)
        return {"X-AIO-API-Key": key} if key else {}


# --------------------------------------------------------------------------- #
# mount + spec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MountSpec:
    """One mount to seed / persist into a container (a storage-layer directive).

    Mounts are how bytes cross into the container (project files, skills) and
    persist back out; the *execution* layer is orthogonal — every tool still runs
    THROUGH the container over HTTP. ``kind`` abstracts the storage backend so the
    same ``MountSpec`` maps to a local ``docker -v`` (``"local-path"`` /
    ``"volume"``) or a distributed NAS / PVC (``"nas"`` / ``"pvc"``) — the **same
    ``target``** in both families, so nothing above the seam ever translates a
    path.
    """

    source: str
    target: str
    mode: str = "rw"  # "rw" | "ro"
    kind: str = "local-path"  # "local-path" | "nas" | "volume" | "pvc"


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """The ``allocate`` input — everything a provider needs to build a container.

    ``mounts`` is the fully-assembled mount list (the workspace mount + the
    skills mounts + any host extension); the SDK manager combines its configured
    base mounts with the per-session workspace mount and hands the result here.
    ``resources`` caps memory / cpus; ``env`` injects extra container environment.
    A distributed provider reads the same fields and maps them to its own
    control-plane API.
    """

    image: str
    mounts: tuple[MountSpec, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# handle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SandboxHandle:
    """A live container binding: durable *addressing* + a non-durable *auth*.

    The addressing triple is the only part that is durable-safe and is what makes
    reconnect work:

    * ``base_url`` — the container's full API root. **Must** tolerate a gateway
      path prefix (``https://gateway/<prefix>``), not merely ``host:port`` — the
      adapter builds URLs as ``base_url + "/v1/..."``, so a gateway works with no
      adapter change.
    * ``sandbox_id`` — names the specific provisioned container. ``""`` on an
      attach-one-container provider that does not mint ids → the ref encodes to a
      bare ``base_url``.
    * ``workdir`` — the container's workspace root (default ``/workspace``); in
      sandbox mode this *is* the fs tools' ``WorkspaceRoot`` (a lexical fence).

    ``auth`` is a live :class:`SandboxAuth` — **never serialized**. A resumed /
    reclaimed session reads the addressing back from ``TaskHostBound.exec_env_ref``
    and rebuilds ``auth`` from the reconnecting host's own config, so the
    credential never enters the durable record.
    """

    base_url: str
    sandbox_id: str
    auth: SandboxAuth
    workdir: str = "/workspace"


# --------------------------------------------------------------------------- #
# the provider seam
# --------------------------------------------------------------------------- #


@runtime_checkable
class SandboxProvider(Protocol):
    """Provision / reap / reconnect a per-session sandbox container.

    Implemented in the host layer (which owns "who runs ``docker`` / a K8s API");
    consumed only by the SDK's
    :class:`~noeta.client.sandbox.SandboxExecEnvManager`.

    * :meth:`allocate` — build a **fresh** container for ``root_task_id`` from
      ``spec`` and return its live :class:`SandboxHandle` (after a readiness
      probe). Called once, eagerly, at session open (``driver.seed_start``).
    * :meth:`release` — tear the container down (idempotent — releasing an
      unknown / already-released id is a no-op). Called at root-task terminal and
      as a shutdown backstop.
    * :meth:`attach` — reconnect to an ALREADY-provisioned container named by a
      recorded ``exec_env_ref`` (never build a new one). Called on resume /
      reclaim, possibly on another host. A local provider can only attach a
      container on its own machine; a container that is gone (host restart /
      cross-machine) raises — a limitation a distributed / NAS backend removes.
    """

    def allocate(self, root_task_id: str, spec: SandboxSpec) -> SandboxHandle: ...

    def release(self, root_task_id: str) -> None: ...

    def attach(self, exec_env_ref: str) -> SandboxHandle: ...


# --------------------------------------------------------------------------- #
# durable ref codec
# --------------------------------------------------------------------------- #

#: Separator packing ``base_url`` and ``sandbox_id`` into the flat durable ref.
#: A URL never contains a bare ``#`` except as a fragment delimiter (which our
#: API base URLs never carry), and we split on the LAST one, so a gateway
#: ``base_url`` with a path prefix round-trips cleanly.
_REF_SEP = "#"


def encode_exec_env_ref(base_url: str, sandbox_id: str) -> str:
    """Pack ``(base_url, sandbox_id)`` into the flat durable ``exec_env_ref``.

    An empty ``sandbox_id`` (a provider that mints no id) encodes to the **bare**
    ``base_url``.
    """
    return f"{base_url}{_REF_SEP}{sandbox_id}" if sandbox_id else base_url


def decode_exec_env_ref(ref: str) -> tuple[str, str]:
    """Split a durable ``exec_env_ref`` into ``(base_url, sandbox_id)``.

    Inverse of :func:`encode_exec_env_ref`: splits on the LAST ``#`` so a gateway
    ``base_url`` (which may carry ``/``-path segments but never a ``#``) is
    preserved. A bare ``base_url`` yields ``sandbox_id == ""``.
    """
    base_url, sep, sandbox_id = ref.rpartition(_REF_SEP)
    if not sep:
        return ref, ""
    return base_url, sandbox_id
