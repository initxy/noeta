# 模型与 provider

怎样让 noeta 用上某个模型：Anthropic、兼容 OpenAI 的接口（vLLM、DeepSeek、各种网关）、OpenAI 的 Responses 接口，或者几个同时用；怎样切换模型、设推理强度，以及给插件在旁边做的杂活挑便宜些的模型。内部怎么路由、为什么这样设计，见 [provider 与模型路由](../how-it-works/models.md)。

## 只用一个 provider

noeta 需要一个模型 id，以及把它发给哪个 provider。每项都可以用环境变量、命令行参数或 `~/.noeta/config.toml` 里的键来设；参数优先于环境变量，环境变量优先于文件（`noeta/cli/config.py`）。

| 设置 | 环境变量 | 参数 | `config.toml` |
|---|---|---|---|
| 模型 id | `NOETA_MODEL` | `--model` | `model` |
| provider 类型 | `NOETA_PROVIDER` | `--provider` | `provider` |
| 接口地址 | `NOETA_BASE_URL` | `--base-url` | `base_url` |
| API 密钥 | `NOETA_API_KEY` | — | `api_key` |
| 推理强度 | `NOETA_EFFORT` | `--effort` | `effort` |
| 模型描述文件 | `NOETA_MODELS_FILE` | — | `models_file` |

上面都没设模型时，还会看设置文件里的 `model` 键（`~/.noeta/settings.json`、已信任项目的 `.noeta/settings.json`），这是 Claude Code 放它的地方。一个都没有，`noeta` 会以 `no model configured` 退出。

### Anthropic

```bash
export NOETA_MODEL=claude-sonnet-5-5 NOETA_API_KEY=sk-ant-...    # 或 ANTHROPIC_API_KEY
noeta
```

`anthropic` 是默认 provider。它的接口地址只写到主机（noeta 自己补上 `/v1/messages`），所以兼容 Messages 接口的网关写成 `NOETA_BASE_URL=https://gateway.example.com`。发出去的请求和 Claude Code 给 API 密钥用户发的一样：同样的 beta 头、自适应思考、提示缓存断点、推理强度。

别名 `opus`、`sonnet`、`haiku`、`fable` 指向当前这一代模型（`noeta/core/models.py` 里的 `ALIASES`）：`--model opus` 就是 `claude-opus-5-5`。

提示缓存默认存活五分钟。在 `config.toml`（或某个 provider 表）里写 `prompt_cache_ttl = "1h"`，改用一小时缓存：每次写缓存贵一些，任务在两轮之间闲着时更划算。

### 兼容 OpenAI 的接口：vLLM、DeepSeek、网关

`openai_compat` 走 `/chat/completions`。接口地址以 `/v1` 结尾，noeta 自己补上 `/chat/completions`。密钥读 `NOETA_API_KEY`，没有就读 `OPENAI_API_KEY`。

```bash
# DeepSeek
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://api.deepseek.com/v1
export NOETA_MODEL=deepseek-flash NOETA_API_KEY=sk-...

# 本地的 vLLM
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=http://localhost:8000/v1
export NOETA_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct NOETA_API_KEY=unused
```

DeepSeek 现在的两个模型 `deepseek-flash` 和 `deepseek-v4-pro` 已经在模型目录里，窗口和标价都填好了；旧名字 `deepseek-chat`、`deepseek-reasoner` 已在 2026-07-24 被 DeepSeek 停用。

noeta 自带的模型目录不认识的模型，会按保守的默认上限跑（上下文窗口 128,000 token，输出 16,384 token），并给一句警告，除非有地方告诉 noeta 它的真实上限：

- 它的 provider 的 `/models` 列表里写了窗口大小（只从你配置的接口读，在 `~/.noeta/cache/models` 缓存一天）；
- provider 表里的 `models` 条目（见下文）；
- 一个模型描述文件：

```json
{"models": [
  {"id": "Qwen/Qwen3-Coder-30B-A3B-Instruct", "context_window": 262144,
   "max_output_tokens": 65536, "supports_vision": false}
]}
```

```bash
export NOETA_MODELS_FILE=~/.noeta/models.json
```

一条记录要两个上限都写才算数；模型目录里已有的模型，写了也不改变什么。记录里还可以写 `"provider": "<表名>"`，指明由哪张 [provider 表](#同时用几个-provider)来服务这个模型；这张表必须在 `config.toml` 里存在，否则 noeta 报配置错误、不启动。

网关要自己的请求头：在 provider 表里写 `headers = {"X-Team" = "infra"}`，默认 provider 则用 `extra_headers`。

### OpenAI Responses

`openai_responses` 走 OpenAI 的 `/v1/responses`。它的接口地址要写完整，也没有专门的密钥环境变量：设 `NOETA_API_KEY` 或 `api_key`。

```bash
export NOETA_PROVIDER=openai_responses NOETA_BASE_URL=https://gateway.example.com/v1/responses
export NOETA_MODEL=gpt-5.5-2026-04-24 NOETA_API_KEY=sk-...
```

密钥放在 `api-key` 请求头里发出去，这是 Azure OpenAI 和同类网关认的写法（`noeta/backends/providers/openai_responses.py`）。OpenAI 自己的 `api.openai.com` 认的是 `Authorization: Bearer …`：在 `config.toml` 里写 `extra_headers = {Authorization = "Bearer sk-..."}` 补上（provider 表里则写在 `headers`）。

## 同时用几个 provider

更多 provider 写成 `config.toml` 里的表。顶层设置仍是默认 provider；写 `provider = "<表名>"` 可以改由某张表当默认。

```toml
model = "claude-sonnet-5-5"            # 默认 provider：anthropic
                                       # （密钥读 NOETA_API_KEY，没有就读 ANTHROPIC_API_KEY）

[providers.deepseek]
family = "openai_compat"               # anthropic | openai_compat | openai_responses
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"     # 直接写值，或 {env:NAME}、{keyring:service/user}
models = ["deepseek-flash", "deepseek-v4-pro"]

[providers.local]
family = "openai_compat"
base_url = "http://localhost:8000/v1"
api_key = "unused"
models = [{id = "Qwen/Qwen3-Coder-30B-A3B-Instruct", context_window = 262144, max_output_tokens = 65536}]
list_models = true                     # 读它的 /models（默认）；false 不读
```

一张表只接受 `family`、`base_url`、`api_key`、`headers`、`models`、`list_models` 和 `prompt_cache_ttl`；写了别的键算配置错误，表名叫 `default` 也算（这个名字留给顶层 provider）。`{env:…}` 和 `{keyring:…}` 在表里的 `api_key` 和 `headers` 中会被替换成真实值，顶层的 `api_key` 和 `extra_headers` 也一样。`{keyring:…}` 需要 `keyring` 这个 extra。

一个模型 id 发给哪个 provider：

1. 哪张表的 `models` 里列了它，或模型描述文件里哪条记录用 `"provider"` 点了名，就发给谁（同一个模型被两个 provider 点名算配置错误）；
2. 否则发给 `/models` 列表里有它的第一个 provider，默认的排第一；
3. 都没有，就发给默认 provider。

配了不止一个 provider 时，这个选择只在任务以某个模型开始、或切换到某个模型时做一次，并和模型 id 一起记进任务配置（`model_route`）。之后每个工人都把这个模型发给记下的那个 provider，不管自己的列表怎么说；如果那个工人上根本没配这个 provider，这次调用就失败，所以每个工人都要用同一份 `config.toml`。只有一个 provider 时什么都不记，除非它的 `/models` 列表给出了这个模型的上限（这时会记下上限，让每个工人按同样的大小组织请求）。

一次对话里混用几家也没问题：某家签过名的推理内容只回放给那一家，别家写的工具调用 id 会改写成 Anthropic 接受的格式。

## 切换模型

- **终端界面里**：`/model` 列出别名和每个 provider 的模型，并标出来自哪个 provider；`/model deepseek-flash` 直接切换。切换从下一次请求起生效，并记进日志，恢复任务时依然有效。
- **网页版和编辑器里**：用模型选择器；ACP 里也可以用 `/model [name]`。
- **无界面模式**：`--model`，或 SDK 的 `set_model` 控制请求。
- **单个子代理**：代理文件里的 `model:`（`inherit`、别名或 id），或模型调用 `Agent` 时传的 `model` 参数；否则子代理用调用者的模型（见[子代理](./subagents.md)）。
- **单个技能**：技能 frontmatter 里的 `model:`，加载它的那一轮剩下的部分都用这个模型（见[技能](./skills-and-agents.md)）。

切换到 noeta 一无所知的模型时，对话里会提示一句（会按哪些默认上限来跑），方便你补一条模型描述。

## 推理强度和思考

推理强度（effort）决定模型想得多深：`low`、`medium`、`high`、`xhigh`、`max`。可以用 `--effort`、`NOETA_EFFORT`、`config.toml` 里的 `effort`、终端界面里的 `/effort`、代理文件或技能里的 `effort:`、或 `Agent` 调用的 `effort` 参数来设。都没设时，请求用模型目录里这个模型的默认值（和 Claude Code 一样），没有默认值就用 `high`（`noeta/core/models.py` 里的 `default_effort`）。

各 provider 的处理：

| provider | 发送为 | 说明 |
|---|---|---|
| `anthropic` | `output_config.effort` | 自适应思考，思考内容以摘要形式返回；旧的思考保留在上下文里 |
| `openai_compat` | `reasoning_effort` | `xhigh` 和 `max` 都变成 `high`；模型的推理文字会显示，但不回传（DeepSeek 收到回传的 `reasoning_content` 会报错） |
| `openai_responses` | `reasoning.effort` | 同样的映射；会请求推理摘要 |

提问里出现 `ultrathink` 这个词，就是要模型想得更深，和 Claude Code 一样：模型会被告知这一点；如果有人在实时跟着这个任务，这一轮剩下的部分改用 `xhigh`（已经这么高就不变；`noeta/toolkit/ultrathink.py`）。Claude Code 的 `--max-thinking-tokens` 和 SDK 的 `set_max_thinking_tokens` 照样接受、不起作用：思考多少由推理强度决定。

## 插件在旁边用的模型

有些插件会在任务自己的轮次之外另外调一次模型。大多默认用任务的模型（例外见下表），你可以给它们指定便宜些的；在 `config.toml` 里作为插件参数来设：

```toml
[plugins.compaction]
compaction_model = "claude-haiku-5-5"    # 压缩上下文时写摘要

[plugins.title]
model = "claude-haiku-5-5"               # 给任务起一次标题

[plugins.permissions]
classifier_model = "claude-sonnet-5-5"   # auto 模式的分类器

[plugins.web]
webfetch_model = "claude-haiku-5-5"      # WebFetch 抓到网页后用它提炼

[plugins.memory]
recall_model = "claude-haiku-5-5"        # 每条消息挑相关记忆文件（不设就不挑）
```

| 插件 | 参数 | 默认 |
|---|---|---|
| `compaction` | `compaction_model` | 任务的模型 |
| `title` | `model` | 任务的模型 |
| `permissions` | `classifier_model` | 任务跑 Claude 模型时用 `claude-sonnet-5`，否则用任务的模型；分类器的模型调用失败时退回任务的模型 |
| `web` | `webfetch_model` | 任务的模型 |
| `memory` | `recall_model` | 无：不做记忆召回 |
| `hooks` | `prompt` 或 `agent` 类型 hook 的 `model` 字段 | 任务的模型 |

这些插件用的模型在调用它的那个工人上解析（不记成路由），所以配了几个 provider 时，要在某张 provider 表的 `models` 或模型描述文件里点名它，这样它在哪台机器上都发给同一家。
