# Swap providers

**Goal:** switch an agent from one LLM provider to another without
rewriting any agent code.

**Before you start:** you have a working agent using one provider (see
[Configure a provider](configure-provider.md)).

## The same recipe, different wiring

Provider neutrality means your agent's identity — system prompt, tools,
permission mode, child agents — never depends on which provider is
serving it. The provider is **wiring**, injected at `Client` or `query`
time. Swap it, and the same `Options` compiles to the same `AgentSpec`.

## Before: Anthropic

```python
from noeta.sdk import Client, Options, query
from noeta.llm.anthropic import AnthropicProvider

options = Options(
    system_prompt="You are a concise assistant.",
    name="concise-bot",
    allowed_tools=None,
)

anthropic = AnthropicProvider(
    model="claude-sonnet-4-5-20250929",
    api_key="sk-ant-…",
)

client = Client(options, provider=anthropic, workspace_dir="./")
```

## After: OpenAI-compatible

```python
from noeta.llm.openai_compat import OpenAICompatProvider

openai = OpenAICompatProvider(
    model="gpt-5.5",
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)

# Same options, same client construction — only the provider changes
client = Client(options, provider=openai, workspace_dir="./")
```

Nothing else changes: same `Options`, same tools, same `Client` usage.
Your recorded history is also portable — EventLog entries are
provider-agnostic, so a session started with Anthropic can resume with
OpenAI.

## Via `query()` (one-shot)

The `query()` convenience function also accepts a `provider` kwarg:

```python
from noeta.sdk import query

result = query(
    options,
    goal="What is the capital of France?",
    provider=openai,  # or anthropic, or any provider
    workspace_dir="./",
)
print(result.answer())
```

## Verify the swap

Run the same goal against both providers and confirm both produce a
terminal answer:

```python
for name, prov in [("anthropic", anthropic), ("openai", openai)]:
    result = query(options, goal="Say hello.", provider=prov, workspace_dir="./")
    print(f"{name}: {result.answer()}")
```

Both should return a successful answer. The exact text will differ
(different models), but both reach a terminal state.

## What does not change

When you swap providers:

- **Tool definitions** — same `@tool` functions, same names, same schemas.
- **Agent identity** — the compiled `AgentSpec` is identical because
  `compile_options` never sees the provider.
- **EventLog format** — recorded events are vendor-neutral. A log
  written with Anthropic can be folded with an OpenAI provider active.
- **Permission model** — same `permission_mode`, same Guards.

## What might change

- **Tool calling format** — the internal protocol normalizes this, but
  edge cases (e.g. parallel tool calls) may behave slightly differently
  across providers.
- **Reasoning / extended thinking** — providers that support extended
  thinking (Anthropic) vs those that do not may produce different
  internal traces.
- **Token counts and pricing** — obviously different per provider.

## See also

- [Provider neutrality](../concepts/provider-neutrality.md) — the design
  behind this
- [Configure a provider](configure-provider.md) — setup for each provider
- [SDK reference](../reference/sdk.md) — `Options`, `Client`, `query`
  signatures
- `examples/swap_provider.py` — runnable demonstration
