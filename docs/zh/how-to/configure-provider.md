# 配置 provider

**目标：** 将 Noeta 连接到真实的 LLM 端点——Anthropic、OpenAI 兼容接口或 OpenAI Responses API。

**开始之前：** 你已安装 Noeta 并通过[快速入门](../tutorials/quickstart.md)运行过 stub provider。你已有所选 provider 的 API 密钥。

## 方案 A：环境变量（coding agent）

为 `python -m noeta.agent` 配置 provider 最简单的方式是通过 `NOETA_AGENT_*` 环境变量：

```bash
# Anthropic
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
python -m noeta.agent

# OpenAI 兼容（任何遵循 OpenAI chat/completions 格式的端点）
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
python -m noeta.agent

# OpenAI Responses API
NOETA_AGENT_PROVIDER=openai-responses \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_API_KEY=sk-… \
python -m noeta.agent
```

对于官方 OpenAI 端点，`NOETA_AGENT_BASE_URL` 是可选的（默认为 `https://api.openai.com/v1`）；对于自托管或第三方 OpenAI 兼容端点，请显式设置。

## 方案 B：JSON 配置文件

不用单独的环境变量，你可以指向一个 JSON 配置文件：

```bash
NOETA_AGENT_CONFIG=./noeta-config.json python -m noeta.agent
```

`noeta-config.json` 的内容如下：

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5-20250929",
  "api_key": "sk-ant-…",
  "workspace": "./my-project",
  "storage_url": "./session.sqlite",
  "write_mode": "dry_run"
}
```

## 方案 C：编程方式（SDK）

直接使用 SDK（`noeta.sdk`）时，向 `Options` 或 `Client` 传入 provider：

```python
from noeta.sdk import Client, Options
from noeta.llm.anthropic import AnthropicProvider

provider = AnthropicProvider(
    model="claude-sonnet-4-5-20250929",
    api_key="sk-ant-…",
)

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=provider,
)

client = Client(options, workspace_dir="./workspace")
```

对于 OpenAI 兼容端点，使用 `OpenAICompatProvider`：

```python
from noeta.llm.openai_compat import OpenAICompatProvider

provider = OpenAICompatProvider(
    model="gpt-5.5",
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)
```

对于 OpenAI Responses API：

```python
from noeta.llm.openai_responses import OpenAIResponsesProvider

provider = OpenAIResponsesProvider(
    model="gpt-5.5",
    api_key="sk-…",
)
```

## 验证是否正常工作

用你的真实 provider 启动代理并发送一条简单消息：

```bash
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
NOETA_AGENT_STORAGE=./test.sqlite \
python -m noeta.agent
```

打开聊天界面，问"2 + 2 等于几？"。如果你收到的是真实 LLM 回复（而不是 stub 的固定回答），说明 provider 已正确连接。检查 trace 视图，确认该轮对话显示了真实的 token 数量和用量。

## 切换 provider

要更换后端，只需更换 provider 实例——无需改动其他代码。参见[切换 provider](swap-providers.md)查看前后对比示例。内部协议是 Provider 中立的，因此你的代理代码、工具和历史记录在不同 provider 之间是可移植的。

三个内置 provider 都会在生成回复时向 Web 界面流式传输 token——无需额外配置。stub provider 不流式传输（它即时回答），所以在纯启动状态下不要期望看到打字效果。

## 故障排查

- **"No provider configured"** — `NOETA_AGENT_PROVIDER` 未设置（默认为 `stub`）或 `Options.provider` 为 `None`。
- **401 / 认证错误** — 检查你的 API 密钥。Anthropic 密钥以 `sk-ant-` 开头；OpenAI 密钥以 `sk-` 开头。
- **模型未找到** — 验证模型名称。Anthropic 模型名称包含日期后缀（例如 `claude-sonnet-4-5-20250929`）。
- **连接超时** — 检查 `NOETA_AGENT_BASE_URL`。对于企业代理，你可能需要设置 `HTTPS_PROXY`。

更多信息参见[故障排查](../operations/troubleshooting.md)。

## 另请参阅

- [Provider 中立性](../concepts/provider-neutrality.md) — 为什么内部协议与供应商无关
- [切换 provider](swap-providers.md) — 前后代码对比示例
- [SDK 参考](../reference/sdk.md) — `Options` 和 `Client` 构造函数参数
