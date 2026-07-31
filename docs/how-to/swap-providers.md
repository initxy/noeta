# Swap providers

This guide shows you how to move a working agent from one LLM provider to
another without rewriting any agent code. You need an agent already running
against one provider — see [Configure a provider](configure-provider.md).

## The same recipe, different wiring

An agent's identity — system prompt, tools, permission mode, child agents — does
not depend on which provider serves it. The provider is **wiring**, injected at
`Client` or `query` time (or set on `Options.provider`, which the explicit kwarg
overrides). Swap it and the same `Options` compiles to the same `AgentSpec`.

## 1. Start from Anthropic

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a concise assistant.",
    name="concise-bot",
    allowed_tools=None,
)

anthropic = AnthropicProvider(api_key="sk-ant-…")

client = Client(
    options,
    provider=anthropic,
    workspace_dir="./",
    model="claude-sonnet-4-6",
)
```

A provider is an adapter for a vendor's wire protocol, not a binding to one
model, so it takes no `model` argument. The model is chosen per session on
`Client(model=…)` / `query(model=…)`, which lets one provider instance serve
many models.

## 2. Change one line to OpenAI-compatible

```python
from noeta.sdk.providers import OpenAICompatProvider

openai = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)

# Same options, same client construction — only the provider changes
client = Client(
    options, provider=openai, workspace_dir="./", model="gpt-5.5-2026-04-24"
)
```

`noeta.sdk.providers` also ships `OpenAIResponsesProvider` for the OpenAI
Responses API, which takes the same `base_url` / `api_key` pair.

The one-shot `query` takes the provider the same way:

```python
from noeta.sdk import query

result = query(
    options,
    goal="What is the capital of France?",
    provider=openai,  # or anthropic, or any provider
    workspace_dir="./",
    model="gpt-5.5-2026-04-24",
)
print(result.answer())
```

## 3. Verify the swap

Run the same goal against both providers and confirm both reach a terminal
answer:

```python
runs = [
    ("anthropic", anthropic, "claude-sonnet-4-6"),
    ("openai", openai, "gpt-5.5-2026-04-24"),
]
for name, prov, model in runs:
    result = query(
        options, goal="Say hello.", provider=prov, workspace_dir="./", model=model
    )
    print(f"{name}: {result.answer()}")
```

```
anthropic: Hello! How can I help you today?
openai: Hi there — what can I do for you?
```

The exact wording differs, of course. What matters is that both reach a terminal
state from the same `Options`, with no change to your code in between.

## What does not change

- **Tool definitions** — same `@tool` functions, same names, same schemas.
- **Agent identity** — the compiled `AgentSpec` is identical because
  `compile_options` never sees the provider.
- **EventLog format** — recorded events carry neutral message shapes, so a log
  written against one vendor folds without that vendor's adapter installed, and
  a session can resume under a different provider.
- **Permission model** — same `permission_mode`, same Guards.

## What might change

- **Tool calling format** — the internal protocol normalizes this, but edge
  cases (parallel tool calls, for instance) may behave slightly differently.
- **Reasoning continuation** — `OpenAICompatProvider` drops re-attached
  thinking blocks unless you construct it with `reasoning_continuation="chat"`;
  `OpenAIResponsesProvider` echoes the encrypted continuation the Responses API
  requires by default. Traces therefore differ across vendors.
- **Token counts and pricing** — different per provider.

## Next steps

- [Configure a provider](configure-provider.md) — per-adapter setup and the
  model catalog
- [Provider neutrality](../concepts/provider-neutrality.md) — the design behind
  this
- [SDK reference](../reference/sdk.md) — `Options`, `Client`, `query` signatures

`examples/swap_provider.py` is a runnable demonstration.
