"""SDK example — run an agent with ``Options`` + ``query``.

Demonstrated SDK capability
---------------------------
:func:`noeta.sdk.query`, the one-shot entrypoint: a recipe, a provider and a
workspace go in; the whole event-envelope stream for one turn comes back. The
envelope stream — not the answer string — is the canonical record of what the
agent did, and :meth:`QueryResult.answer` is the projection off it.

The smallest starting point there is: one built-in tool, no sub-agents, no
custom tools. The provider is scripted so the example needs no API key and no
network; pass a live ``OpenAICompatProvider`` / ``AnthropicProvider`` from
``noeta.sdk.providers`` (with the matching ``model``) to :func:`run` instead.

    python examples/minimal_agent.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider


def _demo_provider() -> FakeLLMProvider:
    """A network-free provider scripted to answer in one turn, no tool use."""
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

    Kept apart from :func:`main` so the smoke test asserts on a value rather
    than on parsed stdout.
    """
    options = Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )

    result = query(
        options,
        goal="Say hello.",
        provider=provider if provider is not None else _demo_provider(),
        workspace_dir=workspace_dir,
        model=model,
    )

    # ``result`` is the envelope list, but reading the answer off it by hand
    # would break on a spilled answer: the ContentRef is only resolvable
    # against the store ``query`` has already torn down. ``answer()`` was
    # materialized while that store was alive, and it raises
    # ``QueryFailedError`` rather than let a failure reason pass for an answer.
    return str(result.answer())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="noeta-minimal-") as tmp:
        answer = run(workspace_dir=Path(tmp))
    print(f"agent answer: {answer!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
