"""translator pure-mapping unit test: LLMRetryScheduled → llm_retry UIEvent.

Triggering a retry end to end needs the LLM provider to really raise a
retryable error (hard to trigger reliably in the mock); the translator is a
pure function, so constructing the envelope object directly and verifying the
mapping is enough.
"""
from __future__ import annotations

from types import SimpleNamespace

from noeta.agent.host.translator import translate


class _Deref:
    """stub deref: returns the ContentRef as-is (llm_retry reads no content)."""

    def __call__(self, ref):  # noqa: D102
        return ref


def test_translate_llm_retry_scheduled():
    """LLMRetryScheduled translates to llm_retry{call_id}, letting the
    frontend clear its streaming buffer."""
    env = SimpleNamespace(
        type="LLMRetryScheduled",
        seq=42,
        payload=SimpleNamespace(call_id="call-abc"),
        task_id="task-x",
    )
    events = translate(env, _Deref())
    assert len(events) == 1
    ev = events[0]
    assert ev.seq == 42
    assert ev.type == "llm_retry"
    assert ev.data == {"call_id": "call-abc"}
