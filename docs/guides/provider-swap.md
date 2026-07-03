# Swapping providers

Noeta is **provider-neutral**: the same `Options` recipe — same prompt,
same tools, same compiled agent identity — runs unchanged against any
provider. The provider is *wiring*, injected at `query()` time; it never
touches the agent's identity.

## The same recipe, two providers

```python
import tempfile
from pathlib import Path

from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options, compile_options, query
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Define the recipe once ---------------------------------------------

def make_recipe() -> Options:
    return Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )

# --- 2. Prove identity is stable -------------------------------------------
#
# compile_options() is a pure function of the recipe. It never reads the
# provider, so two runs with different providers produce identical agent
# specs.

recipe = make_recipe()
compiled_a, _ = compile_options(recipe)
compiled_b, _ = compile_options(recipe)
assert compiled_a == compiled_b  # same recipe → same identity

# --- 3. Run against two different providers --------------------------------

def provider_saying(text: str) -> FakeLLMProvider:
    """Stand-in for a real vendor adapter."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=text)],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )

def extract_answer(envelopes) -> str:
    store = InMemoryContentStore()
    for env in envelopes:
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            return str(answer_from_payload(env.payload, store))
    return ""

with tempfile.TemporaryDirectory(prefix="noeta-swap-") as tmp:
    answer_a = extract_answer(
        query(
            recipe,
            goal="Say hello.",
            provider=provider_saying("Hello from provider A."),
            workspace_dir=Path(tmp),
            model="model-a",
        )
    )
    answer_b = extract_answer(
        query(
            recipe,
            goal="Say hello.",
            provider=provider_saying("Hello from provider B."),
            workspace_dir=Path(tmp),
            model="model-b",
        )
    )

print(f"provider A answer: {answer_a!r}")
print(f"provider B answer: {answer_b!r}")
```

## Real providers

In production, replace `FakeLLMProvider` with a real adapter:

```python
from noeta.providers.openai_compat import OpenAICompatProvider

openai_provider = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-...",
)
```

```python
from noeta.providers.anthropic import AnthropicProvider

anthropic_provider = AnthropicProvider(
    api_key="sk-ant-...",
    default_max_tokens=1024,
)
```

Both implement the same `LLMProvider` interface, so swapping is just a
one-line change at the `query()` call site.

## Launching the agent app with a real provider

When running the full `python -m noeta.agent` app, provider selection is
host config (env vars or JSON file), not code. Two equivalent ways:

### Env vars

```bash
export NOETA_AGENT_PROVIDER=openai
export NOETA_AGENT_BASE_URL=https://api.openai.com/v1
export NOETA_AGENT_API_KEY=sk-...
export NOETA_AGENT_MODEL=gpt-5.5
export NOETA_AGENT_WORKSPACE=./my-project

python -m noeta.agent
```

### Config file (recommended)

Edit `examples/openai-compatible/config.json`:

```json
{
  "provider_id": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.openai.com/v1",
  "api_key": "<your-api-key>",
  "workspace_dir": ".",
  "sqlite_path": ":memory:",
  "host": "127.0.0.1",
  "port": 8765
}
```

Then launch with one variable:

```bash
NOETA_AGENT_CONFIG=examples/openai-compatible/config.json python -m noeta.agent
```

Precedence (low → high): dataclass defaults < `NOETA_AGENT_CONFIG` file <
`NOETA_AGENT_*` env vars. So you can override one field without editing
the file:

```bash
NOETA_AGENT_CONFIG=examples/openai-compatible/config.json \
NOETA_AGENT_MODEL=claude-sonnet-4-5 \
python -m noeta.agent
```

## Key points

- **Provider is never part of the recipe identity.** `compile_options()`
  ignores it entirely. This is the structural guarantee of provider
  neutrality.
- **`model` is also wiring.** It's excluded from identity, so the same
  recipe can target different models per deployment.
- **Never put API keys in `Options`.** Use env vars or a config file for
  the app; pass providers directly when using the SDK in-process.

## Source

- `examples/swap_provider.py` — identity-stability demo
- `examples/openai-compatible/` — config file + launch instructions
- Provider adapters: `packages/noeta-runtime/noeta/providers/`
- `BackendConfig.from_env`: `apps/noeta-agent/noeta/agent/backend/lifecycle.py`
- See also: [Configuration](../reference/configuration.md),
  [ADR: Provider-neutral](../adr/provider-neutral.md)
