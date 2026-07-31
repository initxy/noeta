"""Canonical encoding for dataclasses and tagged value types.

One walker renders Noeta's typed values into a JSON-friendly form whose bytes
are reproducible across runs — snapshot dedup and event payload sizing both
hash them. A value round-trips back into its typed form only if its class
declares ``__canonical_tag__`` and calls :func:`register`; untagged dataclasses
survive as plain field dicts, which is enough for diffing and sizing.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from typing import Any, Callable


TAG_KEY = "__canonical_tag__"

#: Class-level opt-in: field names listed here are OMITTED from the canonical
#: form when their value is ``None``, so a typed value can carry an optional
#: field without its default entering the byte stream (``Message.origin``).
#: Restorers must therefore tolerate the key being absent (a dataclass default
#: does this for free).
OMIT_NONE_KEY = "__canonical_omit_none__"

_NO_OMIT: frozenset[str] = frozenset()


_restorers: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register(tag: str, restorer: Callable[[dict[str, Any]], Any]) -> None:
    _restorers[tag] = restorer


def to_canonical(obj: Any) -> Any:
    """Return a JSON-friendly view of ``obj``."""
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

    Untagged dicts deliberately stay dicts: the consumer rebuilds them from the
    shape its own slice knows.
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
    """Reconstruct dataclass ``cls`` from a field dict, dropping keys it does
    not declare.

    Persisted recordings and snapshot bodies outlive the field set that wrote
    them, and ``cls(**d)`` would crash on an unexpected keyword. Filtering to
    the live fields is the one-way tolerance layer that keeps a suspended task
    foldable, resumable, and inspectable.
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
