"""SDK example — swap the provider, keep the recipe.

Demonstrated SDK capability
---------------------------
Provider neutrality. The provider is wiring injected at :func:`query` time and
:func:`noeta.sdk.compile_options` has no parameter for it, so the compiled
agent identity — prompt, tools, fingerprint — cannot depend on which vendor
answers. That structural fact, not a behavioural coincidence, is what makes
moving a workload between vendors a wiring change rather than a rewrite.

Both providers here are network-free scripts; in a deployment they would be
``OpenAICompatProvider`` and ``AnthropicProvider`` from ``noeta.sdk.providers``.
Their answer texts differ only so the swap shows up in the output.

    python examples/swap_provider.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from noeta.sdk import (
    LLMResponse,
    Options,
    TextBlock,
    Usage,
    compile_options,
    query,
)
from noeta.sdk.testing import FakeLLMProvider


def _provider_saying(text: str) -> FakeLLMProvider:
    """A network-free stand-in for a vendor adapter, answering with ``text``."""
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
    """The single recipe both runs share — rebuilding it would prove nothing."""
    return Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )


def run(*, workspace_dir: Path) -> tuple[str, str, bool]:
    """Run the same recipe against two providers.

    Returns ``(answer_a, answer_b, identity_equal)``; the third value is the
    headline invariant — the compiled agent identity survives the swap.
    """
    recipe = _recipe()

    # Compiled before either run and again after both, because the check is
    # that driving a provider leaves nothing behind on the recipe:
    # compile_options is referentially transparent, so an unequal result would
    # mean a run had mutated the identity plane.
    compiled, _ = compile_options(recipe)

    answer_a = str(
        query(
            recipe,
            goal="Say hello.",
            provider=_provider_saying("Hello from provider A (e.g. OpenAI)."),
            workspace_dir=workspace_dir,
            model="model-a",
        ).answer()
    )
    answer_b = str(
        query(
            recipe,
            goal="Say hello.",
            provider=_provider_saying("Hello from provider B (e.g. Claude)."),
            workspace_dir=workspace_dir,
            model="model-b",
        ).answer()
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
