# 配置 provider

**目标：** 让 Noeta 指向真实的 LLM —— 平台指向一个 OpenAI-Responses 兼容网关，或者你自己的 SDK agent 指向任一受支持的 provider。

**开始之前：** 你已经按[快速开始](../tutorials/quickstart.md)跑过零凭证 mock 模式，手上有网关 URL 和 API key。

## 方案 A：平台（`python -m noeta.agent`）

平台对接 **OpenAI-Responses 兼容网关**（公共 OpenAI API，或任何讲 Responses wire 形态的自托管/厂商网关）。配置 `apps/noeta-agent/.env`：

```dotenv
LLM_PROVIDER=auto
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=sk-…
```

- `LLM_BASE_URL` 是**网关根地址** —— provider 会自动追加 `/responses`。认证走 `api-key` header。
- `LLM_PROVIDER=auto`（默认值）在两个值都设置时使用网关，否则回落到离线 mock，因此空的 `.env` 永远不会导致启动失败。
- 用户可选的模型菜单来自 `apps/noeta-agent/models.json`：`id`、`label`、一个 `default: true`、`efforts`（推理力度档位），以及 —— 针对 SDK 目录不认识的模型 —— `context_window` / `max_output_tokens`，让上下文 compaction 能够生效。

重启服务器，检查实际生效的 provider：

```bash
curl -s http://127.0.0.1:8000/api/v1/health
# {"ok": true, "provider": "openai"}   ← "mock" 表示凭证没有生效
```

[`examples/openai-compatible/`](https://github.com/initxy/noeta/tree/main/examples/openai-compatible)
是这套配置的即抄即用版本。

### 第二个网关

模型可以路由到第二个 Responses 兼容网关（不同主机，`Authorization: Bearer` 认证）：设置 `SECONDARY_LLM_BASE_URL` + `SECONDARY_LLM_API_KEY`，并在 `models.json` 里给要路由的模型标记 `"gateway": "secondary"`。第二网关只能叠加在已激活的主网关之上。参见[配置参考](../reference/configuration.md#llm-gateway)。

## 方案 B：编程方式（SDK）

在 `noeta.sdk` 上构建你自己的 agent 时，provider 是 `Options` 的一个字段。各适配器通过 `noeta.sdk.providers` 导出：

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

对接 OpenAI 兼容的 chat-completions 端点用 `OpenAICompatProvider`；对接 Responses API 用 `OpenAIResponsesProvider`（它的 `base_url` 是**完整的** responses 端点）：

```python
from noeta.sdk.providers import OpenAICompatProvider, OpenAIResponsesProvider

chat = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)
responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses",
    api_key="sk-…",
)
```

离线测试与演示使用 `noeta.sdk.testing` 提供的确定性替身：

```python
from noeta.sdk.testing import FakeLLMProvider
```

## 切换 provider

provider 是**接线，不是身份**：换掉实例，其余一切不变 —— agent 代码、工具和已记录的历史都能在厂商之间搬移。前后对比示例见[切换 provider](swap-providers.md)。

## 故障排查

- **`/health` 显示 `"provider": "mock"`** —— `LLM_BASE_URL` 或 `LLM_API_KEY` 为空（auto 已回落），或者你编辑的 `.env` 不是 `apps/noeta-agent/.env`。环境变量会覆盖文件里的值。
- **401 / 认证错误** —— 检查 key；主网关通过 `api-key` header 认证，第二网关通过 `Authorization: Bearer`。
- **输入框里没有想要的模型** —— 菜单来自 `models.json`，而不是来自网关；把条目加进去即可。
- **自定义模型的上下文一直增长、没有 compaction** —— 给 `models.json` 条目补上 `context_window` / `max_output_tokens`。

## 另请参阅

- [Provider 中立](../concepts/provider-neutrality.md) —— 为什么内部协议与厂商无关
- [配置参考](../reference/configuration.md) —— 覆盖每一个配置键
- [切换 provider](swap-providers.md) —— 前后代码对比示例
