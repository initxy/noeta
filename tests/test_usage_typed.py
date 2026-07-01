"""Typed :class:`Usage` invariants (Noeta-shape usage data format).

The Noeta-shape ``Usage`` replaces the legacy bare ``dict`` on
``LLMResponse.usage`` and on the ``LLMRequestFinishedPayload.usage``
field. It is the shared data foundation for cost accounting and
memory / token management.

Invariants: ``input`` is a **derived property** (``uncached + cache_read +
cache_write``), never stored, never validated in ``__post_init__``;
``visible_output = max(0, output - reasoning_tokens)``. ``Usage`` is a
frozen dataclass so it cannot be mutated after construction.
"""

from __future__ import annotations

import dataclasses

import pytest

from noeta.protocols.canonical import (
    from_canonical,
    from_canonical_bytes,
    to_canonical,
    to_canonical_bytes,
)
from noeta.protocols.messages import Usage


def test_input_is_derived_sum_of_uncached_cache_read_cache_write() -> None:
    u = Usage(uncached=10, cache_read=5, cache_write=0, output=20)
    assert u.input == 15


def test_input_recomputes_with_cache_write() -> None:
    u = Usage(uncached=100, cache_read=25, cache_write=50, output=200)
    assert u.input == 175


def test_empty_usage_is_all_zero_and_invariant_holds() -> None:
    u = Usage()
    assert u.uncached == 0
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.output == 0
    assert u.reasoning_tokens == 0
    assert u.input == 0
    assert u.visible_output == 0


def test_visible_output_subtracts_reasoning_tokens() -> None:
    u = Usage(output=100, reasoning_tokens=30)
    assert u.visible_output == 70


def test_visible_output_clamps_to_zero_when_reasoning_exceeds_output() -> None:
    u = Usage(output=10, reasoning_tokens=40)
    assert u.visible_output == 0


def test_usage_is_frozen() -> None:
    u = Usage(uncached=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.uncached = 99  # type: ignore[misc]


def test_post_init_does_not_raise_on_any_token_combination() -> None:
    # D-A1: no invariant validation in __post_init__ — input is derived,
    # so there is no "illegal" combination to reject.
    u = Usage(uncached=3, cache_read=7, cache_write=11, output=2, reasoning_tokens=1)
    assert u.input == 21


# ---------------------------------------------------------------------------
# canonical round-trip — Usage carries no __canonical_tag__; it rides inside
# LLMResponse / LLMRequestFinishedPayload and is rebuilt by the consumer
# (runtime/llm._deserialize_response, sqlite restorer). to_canonical expands
# it into a *stored-fields* dict; the derived ``input`` property is NOT a
# stored field and must not leak into the canonical bytes.
# ---------------------------------------------------------------------------


def test_to_canonical_expands_only_stored_fields_no_input_key() -> None:
    u = Usage(uncached=10, cache_read=5, cache_write=2, output=20, reasoning_tokens=3)
    canon = to_canonical(u)
    assert canon == {
        "uncached": 10,
        "cache_read": 5,
        "cache_write": 2,
        "output": 20,
        "reasoning_tokens": 3,
    }
    # Derived property must not be serialized — keeps cost ① free to read it
    # without it ever entering the canonical contract / byte stream.
    assert "input" not in canon


def test_round_trip_via_dict_rebuilds_equal_usage() -> None:
    u = Usage(uncached=10, cache_read=5, cache_write=2, output=20, reasoning_tokens=3)
    # Untagged dataclass: from_canonical leaves it a dict; the consumer
    # rebuilds Usage from the stored-field dict. Verify that path is lossless.
    rebuilt = Usage(**from_canonical(to_canonical(u)))
    assert rebuilt == u
    assert rebuilt.input == u.input


def test_round_trip_via_bytes_rebuilds_equal_usage() -> None:
    u = Usage(uncached=7, cache_read=0, cache_write=0, output=4, reasoning_tokens=1)
    rebuilt = Usage(**from_canonical_bytes(to_canonical_bytes(u)))
    assert rebuilt == u
