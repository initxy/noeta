"""``SandboxProvider`` — the per-session container provisioning seam (v2, D2).

v1 addressed **one** external AIO Sandbox container by its ``base_url`` and had
every session on a host share it (``SandboxExecEnvConfig`` + the base_url-keyed
manager). v2 makes the sandbox **per root-task tree**: a fresh container is
provisioned when a session opens and torn down when it ends. This module defines
the seam the SDK consumes and the agent layer implements — the SDK never shells
out to ``docker`` / a K8s API itself (that "who provisions the container" work is
the product's, per the runtime/SDK/agent split, D1):

* :class:`SandboxProvider` — ``allocate`` / ``release`` / ``attach``. The one
  interface the SDK's :class:`~noeta.client.sandbox.SandboxExecEnvManager` drives.
* :class:`SandboxHandle` — what ``allocate`` returns: the *addressing* half
  (``base_url`` / ``sandbox_id`` / ``workdir``, all durable-safe) plus a live
  :class:`SandboxAuth` strategy that is **never** serialized.
* :class:`SandboxSpec` / :class:`MountSpec` — the ``allocate`` input: image,
  resource caps, and the configurable mount list (workspace + skills + any
  extension). ``MountSpec.kind`` abstracts the storage backend so a Local
  ``docker -v`` and a Distributed NAS ``fuse_mount_params`` are the same shape.
* :class:`SandboxAuth` / :class:`StaticApiKeyAuth` — auth is a **strategy**, not
  a static key, so a TAE ``JwtBearerAuth`` (short-lived Bearer JWT) drops in with
  no seam change (D8 / D5-NAS pre-requisite (a)).

Two provider families implement this (D2): **Local** (the container runs on the
worker's own Docker daemon — ``LocalDockerSandboxProvider``, this round) and
**Distributed** (a remote node / cluster — ``TaeSandboxProvider`` / a K8s
provider, later). They differ only in where the container runs and how it is
addressed / authed / mounted; the SDK above this seam is identical for both.

**Durable ref encoding (D4).** A session's bound container is recorded on
``TaskHostBound.exec_env_ref`` so a resumed / reclaimed session reconnects to the
SAME container. v1 stored the bare ``base_url``; v2 must also carry the
``sandbox_id`` that names the specific provisioned container. To avoid reshaping
the canonical serialization (the ref stays a flat ``str`` with the existing
``__canonical_omit_none__`` omit-when-``None`` idiom), the two parts are packed
as ``"{base_url}#{sandbox_id}"`` and split on the LAST ``#``
(:func:`encode_exec_env_ref` / :func:`decode_exec_env_ref`). An empty
``sandbox_id`` (the v1 attach-one-container path, :class:`SandboxHandle` with no
minted id) encodes to the **bare** ``base_url`` — byte-identical to a v1 ref, so
an attach deployment's recordings are unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable


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
# auth strategy (D8 / D5-NAS pre-requisite (a))
# --------------------------------------------------------------------------- #


@runtime_checkable
class SandboxAuth(Protocol):
    """How a session authenticates to its container — a **strategy**, not a key.

    :meth:`connect_headers` is called **per HTTP request** (D8: the AIO adapter
    no longer fixes the auth header at construction time), so a short-lived
    credential is minted fresh each call. v1's :class:`StaticApiKeyAuth` returns
    a constant ``X-AIO-API-Key`` header; a TAE ``JwtBearerAuth`` would mint a
    short Bearer JWT here. The value rides only on the wire — never recorded,
    logged, or serialized (D5). Because it is a live object, a
    :class:`SandboxHandle` does not serialize it; a reconnecting host rebuilds
    the strategy from its own local config.
    """

    def connect_headers(self) -> dict[str, str]: ...


class StaticApiKeyAuth:
    """The v1 auth strategy: a static ``SANDBOX_API_KEY`` from the environment.

    The key is read from ``env_name`` **at connect time** (never at
    construction / never held on a durable object), matching v1's "the secret is
    fetched only on the wire, never in a config / log / event" rule (D5). An
    unset env var yields no header (an unauthenticated container).
    """

    __slots__ = ("_env_name",)

    def __init__(self, env_name: str = "SANDBOX_API_KEY") -> None:
        self._env_name = env_name

    def connect_headers(self) -> dict[str, str]:
        key = os.environ.get(self._env_name)
        return {"X-AIO-API-Key": key} if key else {}


# --------------------------------------------------------------------------- #
# mount + spec (D5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MountSpec:
    """One mount to seed / persist into a container (a storage-layer directive).

    Mounts are how bytes cross into the container (project files, skills) and
    persist back out; the *execution* layer is orthogonal — every tool still
    runs THROUGH the container over HTTP (D5, "execution 全经容器"). ``kind``
    abstracts the storage backend so the same ``MountSpec`` maps to a Local
    ``docker -v`` (``"local-path"`` / ``"volume"``) or a Distributed NAS /
    PVC (``"nas"`` → TAE ``fuse_mount_params``, ``"pvc"``) — the **same
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
    built-in / global skills mounts + any deployment extension); the SDK manager
    combines its configured base mounts with the per-session workspace mount and
    hands the result here. ``resources`` caps memory / cpus; ``env`` injects
    extra container environment. A Distributed provider reads the same fields and
    maps them to its own control-plane API (K8s Pod spec / TAE session params).
    """

    image: str
    mounts: tuple[MountSpec, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# handle (D4 / D8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SandboxHandle:
    """A live container binding: durable *addressing* + a non-durable *auth*.

    The addressing triple is what makes reconnect work and is the only part that
    is durable-safe:

    * ``base_url`` — the container's full API root. **Must** tolerate a gateway
      path prefix (``https://gateway/<prefix>``), not merely ``host:port`` — the
      AIO adapter already builds URLs as ``base_url + "/v1/..."`` so a
      Distributed gateway works with no adapter change (D5-NAS pre-requisite
      (b)).
    * ``sandbox_id`` — names the specific provisioned container (the v2 addition
      over v1's base_url-only ref). ``""`` on an attach-one-container provider
      that does not mint ids → the ref encodes to a bare ``base_url``,
      byte-identical to v1.
    * ``workdir`` — the container's workspace root (default ``/workspace``); in
      sandbox mode this *is* the fs tools' ``WorkspaceRoot`` (a lexical fence).

    ``auth`` is a live :class:`SandboxAuth` — **never serialized** (D5/D8). A
    resumed / reclaimed session reads the addressing back from
    ``TaskHostBound.exec_env_ref`` and rebuilds ``auth`` from the reconnecting
    host's own config, so the credential never enters the durable record.
    """

    base_url: str
    sandbox_id: str
    auth: SandboxAuth
    workdir: str = "/workspace"


# --------------------------------------------------------------------------- #
# the provider seam (D2)
# --------------------------------------------------------------------------- #


@runtime_checkable
class SandboxProvider(Protocol):
    """Provision / reap / reconnect a per-session sandbox container.

    Implemented in the agent layer (the product owns "who runs ``docker`` / a
    K8s API"); consumed only by the SDK's
    :class:`~noeta.client.sandbox.SandboxExecEnvManager`.

    * :meth:`allocate` — build a **fresh** container for ``session_root_id`` from
      ``spec`` and return its live :class:`SandboxHandle` (after a readiness
      probe). Called once, eagerly, at session open (``driver.seed_start``).
    * :meth:`release` — tear the container down (idempotent — releasing an
      unknown / already-released id is a no-op). Called at root-task terminal and
      as a shutdown backstop.
    * :meth:`attach` — reconnect to an ALREADY-provisioned container named by a
      recorded ``exec_env_ref`` (never build a new one). Called on resume /
      reclaim, possibly on another host. A Local provider can only attach a
      container on its own machine; a container that is gone (host restart /
      cross-machine) raises — the Docker-local limitation a Distributed / NAS
      backend removes (D5-NAS / R2).
    """

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle: ...

    def release(self, session_root_id: str) -> None: ...

    def attach(self, exec_env_ref: str) -> SandboxHandle: ...


# --------------------------------------------------------------------------- #
# durable ref codec (D4)
# --------------------------------------------------------------------------- #

#: Separator packing ``base_url`` and ``sandbox_id`` into the flat durable ref.
#: A URL never contains a bare ``#`` except as a fragment delimiter (which our
#: API base URLs never carry), and we split on the LAST one, so a gateway
#: ``base_url`` with a path prefix round-trips cleanly.
_REF_SEP = "#"


def encode_exec_env_ref(base_url: str, sandbox_id: str) -> str:
    """Pack ``(base_url, sandbox_id)`` into the flat durable ``exec_env_ref``.

    An empty ``sandbox_id`` (an attach-one-container provider that mints no id)
    encodes to the **bare** ``base_url`` — byte-identical to a v1 ref, so an
    attach deployment's ``TaskHostBound`` bytes are unchanged.
    """
    return f"{base_url}{_REF_SEP}{sandbox_id}" if sandbox_id else base_url


def decode_exec_env_ref(ref: str) -> tuple[str, str]:
    """Split a durable ``exec_env_ref`` into ``(base_url, sandbox_id)``.

    Inverse of :func:`encode_exec_env_ref`: splits on the LAST ``#`` so a
    gateway ``base_url`` (which may itself carry ``/``-path segments but never a
    ``#``) is preserved. A bare ``base_url`` (v1 / attach) yields ``("", ...)``
    → ``sandbox_id == ""``.
    """
    base_url, sep, sandbox_id = ref.rpartition(_REF_SEP)
    if not sep:
        return ref, ""
    return base_url, sandbox_id
