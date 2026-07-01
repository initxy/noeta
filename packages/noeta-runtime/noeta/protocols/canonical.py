"""Canonical encoding for dataclasses and tagged value types.

A single walker shared by every place that needs to render Noeta's
typed values into a stable, JSON-friendly form:

* Snapshot bodies stored in ContentStore (hash stability).
* EventLog 4-KB payload sizing (enforcement in memory.py).

Typed value classes (``ContentRef``, ``SubtaskCompleted``,
``HumanResponseReceived``, ``TimerFired``, ``SubtaskResult``) declare a
class-level ``__canonical_tag__`` and register a restorer; the round
trip ``to_canonical_bytes → from_canonical_bytes`` reconstructs the
typed object. Untagged dataclasses survive as plain field dicts —
fine for diffing and snapshot sizing, where structural equality is
what matters.

Adding a new typed value: give the dataclass a ``__canonical_tag__``
attribute and call :func:`register` at module load. No other module
needs to change.

Growing an *optional* field on an existing typed value without
breaking byte-equal resume of old recordings: list the field name in a
class-level ``__canonical_omit_none__`` frozenset — the canonical form
then omits it whenever its value is ``None`` (``Message.origin``).
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from typing import Any, Callable


TAG_KEY = "__canonical_tag__"

#: Class-level opt-in (mirrors ``__canonical_tag__``): field names listed
#: here are OMITTED from the canonical form when their value is ``None``.
#: This is how a typed value grows a new optional field without breaking
#: byte-equal resume of recordings made before the field existed — the
#: default never enters the byte stream (``Message.origin``).
#: The restorer must therefore tolerate the key being absent (a dataclass
#: default does this for free).
OMIT_NONE_KEY = "__canonical_omit_none__"

_NO_OMIT: frozenset[str] = frozenset()


_restorers: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register(tag: str, restorer: Callable[[dict[str, Any]], Any]) -> None:
    """Register a callable that rebuilds a typed value from its fields."""
    _restorers[tag] = restorer


def to_canonical(obj: Any) -> Any:
    """Return a JSON-friendly view of ``obj``.

    Dataclass instances become field dicts. Dataclasses carrying a
    ``__canonical_tag__`` class attribute add a ``__canonical_tag__``
    key so ``from_canonical`` can restore them. Lists / tuples /
    dicts walk recursively. Scalars pass through.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        tag = getattr(type(obj), TAG_KEY, None)
        omit_none = getattr(type(obj), OMIT_NONE_KEY, _NO_OMIT)
        result: dict[str, Any] = {}
        if tag is not None:
            result[TAG_KEY] = tag
        for fld in fields(obj):
            value = getattr(obj, fld.name)
            if value is None and fld.name in omit_none:
                continue
            result[fld.name] = to_canonical(value)
        return result
    if isinstance(obj, dict):
        return {k: to_canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_canonical(v) for v in obj]
    return obj


def from_canonical(obj: Any) -> Any:
    """Inverse of :func:`to_canonical` for tagged values.

    Tagged dicts are restored via their registered factory. Untagged
    dicts stay as dicts (untagged dataclasses do not round-trip into
    typed objects — that is by design: the consumer rebuilds them
    using its slice's known shape).
    """
    if isinstance(obj, dict):
        tag = obj.get(TAG_KEY)
        if tag is not None and tag in _restorers:
            payload = {
                k: from_canonical(v) for k, v in obj.items() if k != TAG_KEY
            }
            return _restorers[tag](payload)
        return {k: from_canonical(v) for k, v in obj.items() if k != TAG_KEY}
    if isinstance(obj, list):
        return [from_canonical(v) for v in obj]
    return obj


def restore_dataclass(cls: Any, d: dict[str, Any]) -> Any:
    """Reconstruct dataclass ``cls`` from a field dict, dropping keys the
    current class no longer declares.

    Old persisted recordings / snapshot bodies may carry fields that a later
    runtime version retired (e.g. the now-removed ``*_fingerprint`` keys on
    ``AgentBound`` / ``TaskHostBound`` / ``GovernanceState``). A plain
    ``cls(**d)`` would crash on the unexpected keyword; filtering to the live
    field set lets it succeed instead. This is the one-way tolerance layer that
    keeps an old suspended task foldable / resumable / inspectable after a field
    is removed — new recordings never carry the dropped key, so the filter is a
    no-op for them.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


def to_canonical_bytes(obj: Any) -> bytes:
    """Canonical bytes: sort_keys + compact separators + UTF-8.

    Stable across runs, so the content hash of equivalent objects
    matches; snapshot dedup relies on this.
    """
    return json.dumps(
        to_canonical(obj),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def from_canonical_bytes(body: bytes) -> Any:
    return from_canonical(json.loads(body.decode("utf-8")))
