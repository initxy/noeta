"""SDK example — swap the provider, keep the recipe (provider neutrality).

Demonstrated SDK capability
---------------------------
Provider neutrality. The same
:class:`noeta.client.Options` recipe — same prompt, same tools, same
compiled agent identity — runs unchanged against any provider. The provider
is *wiring*, injected at :func:`query` time; it never touches the agent's
identity. This is what lets a library user move a workload from one LLM
vendor to another without rewriting their agent.

This example runs the identical recipe twice against two different
provider instances and shows both produce a terminal answer. In a real
deployment those two would be, e.g.::

    from noeta.providers.openai_compat import OpenAICompatProvider
    from noeta.providers.anthropic import AnthropicProvider

    openai = OpenAICompatProvider(base_url=..., api_key=...)
    claude = AnthropicProvider(api_key=..., default_max_tokens=1024)

Here both are offline :class:`FakeLLMProvider` instances (no API key) so
the example — and its smoke test — run with no network. The point is
structural: ``compile_options`` never sees the provider, so the compiled
agent identity is identical across the two runs.

    python examples/swap_provider.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.client import Options, compile_options, query
from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider


def _provider_saying(text: str) -> FakeLLMProvider:
    """A provider scripted to answer with ``text`` in one turn.

    Stands in for a real vendor adapter; the two calls in :func:`run`
    use two different texts purely to make the swap visible in output.
    """
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=text)],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def _recipe() -> Options:
    """The one provider-agnostic recipe used for both runs."""
    return Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )


def _answer_from(envelopes) -> str:
    store = InMemoryContentStore()
    for env in envelopes:
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            return str(answer_from_payload(env.payload, store))
    return ""


def run(*, workspace_dir: Path) -> tuple[str, str, bool]:
    """Run the same recipe against two providers.

    Returns ``(answer_a, answer_b, identity_equal)``. The third value is the
    headline invariant: the compiled agent identity does not depend on which
    provider is wired in.
    """
    recipe = _recipe()

    # The provider is never read by compile_options — same recipe, same
    # compiled agent identity, regardless of vendor.
    compiled, _ = compile_options(recipe)

    answer_a = _answer_from(
        query(
            recipe,
            goal="Say hello.",
            provider=_provider_saying("Hello from provider A (e.g. OpenAI)."),
            workspace_dir=workspace_dir,
            model="model-a",
        )
    )
    answer_b = _answer_from(
        query(
            recipe,
            goal="Say hello.",
            provider=_provider_saying("Hello from provider B (e.g. Claude)."),
            workspace_dir=workspace_dir,
            model="model-b",
        )
    )

    compiled_again, _ = compile_options(recipe)
    identity_equal = compiled == compiled_again
    return answer_a, answer_b, identity_equal


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-swap-") as tmp:
        answer_a, answer_b, same = run(workspace_dir=Path(tmp))
    print(f"provider A answer: {answer_a!r}")
    print(f"provider B answer: {answer_b!r}")
    print(f"recipe identity stable across providers: {same}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
