"""SDK example — run a minimal agent through ``Options`` + ``query``.

Demonstrated SDK capability
---------------------------
The one-shot entrypoint :func:`noeta.client.query`: hand it an
:class:`noeta.client.Options` recipe + a provider + a workspace, get back
the full Noeta event-envelope stream for a single turn — the canonical,
machine-readable record of everything the agent did. The final answer
rides the terminal ``TaskCompleted`` envelope, read out here with the
public :func:`noeta.protocols.events.answer_from_payload`.

This is the smallest possible "hello, agent" — one built-in ``read``
tool, no sub-agents, no custom tools. It is the right starting point for
a library user who just wants to drive a model against a workspace and
read what happened.

Running it
----------
The example ships with an offline :class:`FakeLLMProvider` so it runs with
no API key and no network — every call is a pre-scripted response, which
is also what the smoke test relies on. To point it at a real model,
replace ``_demo_provider()`` with one of the real adapters::

    from noeta.providers.openai_compat import OpenAICompatProvider
    provider = OpenAICompatProvider(base_url=..., api_key=...)

    # or
    from noeta.providers.anthropic import AnthropicProvider
    provider = AnthropicProvider(api_key=..., default_max_tokens=1024)

then pass ``model="<your-model-id>"`` to :func:`run`.

    python examples/minimal_agent.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.client import Options, query
from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider


def _demo_provider() -> FakeLLMProvider:
    """An offline provider scripted to answer in one turn (no tool use).

    Swap this for ``OpenAICompatProvider`` / ``AnthropicProvider`` (see the
    module docstring) to drive a real model.
    """
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Hello from a minimal Noeta agent!")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def run(*, provider=None, workspace_dir: Path, model: str = "stub-model") -> str:
    """Drive one turn and return the agent's final answer string.

    Factored out of :func:`main` so the smoke test can assert on the
    return value without going through ``stdout``.
    """
    options = Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )

    envelopes = query(
        options,
        goal="Say hello.",
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
    )

    # The final answer rides the terminal TaskCompleted envelope. The
    # FakeLLMProvider answers inline, so an empty store suffices to deref
    # it; a real run resolves the same way against the live store.
    answer = ""
    store = InMemoryContentStore()
    for env in envelopes:
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            answer = str(answer_from_payload(env.payload, store))
    return answer


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-minimal-") as tmp:
        answer = run(workspace_dir=Path(tmp))
    print(f"agent answer: {answer!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
