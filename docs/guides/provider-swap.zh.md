# 切换提供者 { #swapping-providers }

Noeta 是**提供者中立**的：相同的 `Options` 配方——相同的 prompt、相同的工具、相同的编译代理身份——在任何 provider 上运行都不变。Provider 是*接线*，在 `query()` 时注入；它从不触及代理的身份。

## 同一配方，两个提供者 { #the-same-recipe-two-providers }

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

## 真实提供者 { #real-providers }

在生产环境中，将 `FakeLLMProvider` 替换为真实 adapter：

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

两者都实现相同的 `LLMProvider` 接口，因此切换只是在 `query()` 调用处的一行改动。

## 使用真实提供者启动代理应用 { #launching-the-agent-app-with-a-real-provider }

运行完整的 `python -m noeta.agent` 应用时，provider 选择是主机配置（环境变量或 JSON 文件），而非代码。两种等效方式：

### 环境变量 { #env-vars }

```bash
export NOETA_AGENT_PROVIDER=openai
export NOETA_AGENT_BASE_URL=https://api.openai.com/v1
export NOETA_AGENT_API_KEY=sk-...
export NOETA_AGENT_MODEL=gpt-5.5
export NOETA_AGENT_WORKSPACE=./my-project

python -m noeta.agent
```

### 配置文件（推荐） { #config-file-recommended }

编辑 `examples/openai-compatible/config.json`：

```json
{
  "provider_id": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.openai.com/v1",
  "api_key": "<your-api-key>",
  "workspace_dir": ".",
  "storage_url": ":memory:",
  "host": "127.0.0.1",
  "port": 8765
}
```

然后用一个变量启动：

```bash
NOETA_AGENT_CONFIG=examples/openai-compatible/config.json python -m noeta.agent
```

优先级（低 → 高）：dataclass 默认值 < `NOETA_AGENT_CONFIG` 文件 < `NOETA_AGENT_*` 环境变量。因此你可以在不编辑文件的情况下覆盖一个字段：

```bash
NOETA_AGENT_CONFIG=examples/openai-compatible/config.json \
NOETA_AGENT_MODEL=claude-sonnet-4-5 \
python -m noeta.agent
```

## 要点 { #key-points }

- **Provider 永远不是配方身份的一部分。** `compile_options()` 完全忽略它。这是提供者中立的结构性保证。
- **`model` 也是接线。** 它被排除在身份之外，因此同一配方可以在每次部署中针对不同模型。
- **永远不要把 API key 放在 `Options` 里。** 应用使用环境变量或配置文件；进程内使用 SDK 时直接传递 providers。

## 来源 { #source }

- `examples/swap_provider.py` —— 身份稳定性演示
- `examples/openai-compatible/` —— 配置文件 + 启动说明
- Provider adapters：`packages/noeta-runtime/noeta/providers/`
- `BackendConfig.from_env`：`apps/noeta-agent/noeta/agent/backend/lifecycle.py`
- 另见：[配置](../reference/configuration.md)、[ADR：提供者中立](../adr/provider-neutral.md)
