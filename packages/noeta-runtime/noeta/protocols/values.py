"""Value objects used across all layers (L0).

These are immutable structural types — pure data with no behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from noeta.protocols.canonical import register


# A single EventEnvelope's inline payload is capped at 4 KB so that
# EventLog backends can index, replicate, and audit streams without
# large-body special cases. This is an **EventLog-side** ceiling.
# ContentStore has *no* equivalent cap — bodies larger than this must
# be put into ContentStore and referenced through :class:`ContentRef`.
#
# Adapter modules (``noeta.storage.memory``, ``noeta.storage.sqlite.eventlog``)
# may re-export a local alias for backward compatibility, but the L0
# protocol surface uses only this precise name to keep the meaning
# unambiguous.
EVENT_PAYLOAD_MAX_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Reference into a ContentStore.

    Field semantics:

    * ``hash`` — content-addressed storage key, hex-encoded SHA-256
      (64 chars), **computed by ContentStore** at ``put`` time.
      Identical bodies always produce identical ``hash``.
    * ``size`` — ``len(body)``, also computed by ContentStore at
      ``put`` time. Stored as metadata; **not** used for storage
      dedup or ``get`` lookup.
    * ``media_type`` — caller's description of how to interpret the
      bytes. Participates in :class:`ContentRef` dataclass equality
      but **NOT** in storage dedup. Two puts of the same body with
      different ``media_type`` share one storage row (the first put's
      ``media_type`` is recorded on the row); each call returns a
      fresh :class:`ContentRef` carrying the caller's requested
      ``media_type``.

    ``ContentStore.get(ref)`` looks up by ``ref.hash`` only — the
    ``size`` and ``media_type`` carried on the ref are **not**
    validated against the stored row. Callers that need to verify
    ref consistency must do so themselves.
    """

    hash: str
    size: int
    media_type: str

    __canonical_tag__ = "content_ref"


register("content_ref", lambda f: ContentRef(**f))


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting, and which models they may select (L0).

    The **minimal** authorization value: an ``identity`` string and the
    ``allowed_models`` set the identity is sanctioned to bind. Issue 06
    keeps this deliberately small — capabilities / allowed_side_effects /
    delegation chains are **deferred** (the same "more fields land
    alongside their consumers" discipline as
    :class:`noeta.protocols.events.TaskCreatedPayload`), so nothing here
    pre-commits a richer authorization model.

    ``allowed_models`` is the authorization half of model selection: the
    driver/server validates ``selector ∈ principal.allowed_models ∩
    deployment-allowlist`` *before* any ``ModelBound`` is emitted. Two
    canonical shapes:

    * **CLI local principal** — ``identity="local"`` with
      ``allowed_models=⊤`` (``allows_any=True``): the CLI runs on the
      user's own machine with the user's own credentials, so there is **no
      trust boundary** and every selector is allowed.
    * **Web principal** — comes from the authenticated session; its
      ``allowed_models`` is the explicit set that session may bind.

    ``allows_any`` models the ⊤ (top / unbounded) set without enumerating
    every provider model. When ``True``, ``allowed_models`` is ignored by
    :meth:`permits` and any selector passes the principal half of the check
    (the deployment allowlist still applies). ``Principal`` itself is **not
    serialized into any event payload** — only the durable
    ``principal_identity`` string rides ``ModelBound`` — so the
    ``frozenset`` field never needs a canonical-encoding restorer.
    """

    identity: str
    allowed_models: frozenset[str] = frozenset()
    allows_any: bool = False

    def permits(self, selector: str) -> bool:
        """Whether this principal is sanctioned to bind ``selector``.

        ⊤ principals (``allows_any``) permit every selector; otherwise the
        selector must be an explicit member of ``allowed_models``. This is
        the *principal* half only — the caller still intersects with the
        deployment allowlist.
        """
        return self.allows_any or selector in self.allowed_models


#: The CLI's local principal: ``⊤`` (no trust boundary). The
#: user's own machine + credentials, so every model selector is permitted.
#: Old recordings (no ``ModelBound``) fold to this principal — byte-equal,
#: no drift.
LOCAL_PRINCIPAL = Principal(identity="local", allows_any=True)
