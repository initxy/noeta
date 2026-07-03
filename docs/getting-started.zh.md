# 快速开始

两条路径：一条无需 API key 的 90 秒冒烟测试（验证安装和接线），以及一条真实 provider 演练（展示 Noeta 的核心能力——子任务挂起/恢复）。

## 前置条件

* Python 3.11+
* `pip` 或 `uv`
* 本仓库的本地 checkout（Noeta 在第二阶段不发布到 PyPI）

## 安装

```bash
# 本地 checkout（推荐用于评估）
uv pip install -e apps/noeta-agent

# 直接从 git 安装（noeta-agent 应用壳子目录）
pip install "noeta-agent @ git+<https://github.com/your/repo.git>#subdirectory=apps/noeta-agent"
```

安装后，`python -m noeta.agent` 是入口：它启动官方编码代理（默认使用离线 stub provider）并提供附带的 Web 应用。没有 `noeta` 控制台脚本——运行时通过 `NOETA_AGENT_*` 环境变量配置，而非位置 CLI 参数。

> 注意：PyPI 上的 `noeta`、`noeta-sdk` 和 `noeta-agent` 名称已被无关项目占用。在项目选定发布名称之前，请从本地 checkout 或 git URL 安装。

## 路径 1 —— 90 秒无 key 冒烟测试

`stub` provider 返回一个确定性的两轮 LLM 替身——无需 API key，无需网络调用。启动离线运行器：

```bash
python -m noeta.agent   # 绑定操作系统分配的端口；Ctrl-C 停止
```

或在进程内驱动后端——构建它、验证它在服务、关闭它：

<!-- runnable: smoke -->
```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# 默认值完全离线：stub provider、内存存储。port=0 绑定操作系统分配的端口。
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
finally:
    shutdown()
```

后端在不到一秒内绑定一个临时端口并提供附带的 Web 应用。

记录完全保存在内存中；不会持久化任何内容。

## 路径 2 —— 真实 provider 演示（需要 API key）

此路径针对真实的 OpenAI 兼容或 Anthropic 端点演练子任务挂起/恢复。

```bash
# OpenAI 兼容
NOETA_OPENAI_BASE_URL=https://api.openai.com/v1 \
NOETA_OPENAI_API_KEY=sk-… \
NOETA_OPENAI_MODEL=gpt-4o-mini \
python examples/_internal/real_provider_subtask_demo.py
```

```bash
# Anthropic（演示默认提供 max_tokens=1024；如需不同上限请用 NOETA_MAX_TOKENS=… 覆盖）
NOETA_PROVIDER=anthropic \
NOETA_API_KEY=sk-ant-… \
NOETA_MODEL=claude-3-5-sonnet-20240620 \
python examples/_internal/real_provider_subtask_demo.py
```

演示流程：

1. 生成一个父任务（脚本化 policy），它立即生成一个子任务（使用真实 provider 的 ReAct policy）。
2. 父任务挂起等待子任务；子任务运行真实 LLM，调用 `echo` 工具，然后完成。
3. 父任务通过唤醒-恢复路径被唤醒（`Lease.wake_event` 携带子任务的 `SubtaskResult`），执行其第二个脚本化决策，然后终止。

缺少必需的环境变量会打印 `skipped: …` 并以 0 退出，因此即使凭据不可用，该脚本在 CI 中运行也是安全的。

## 使用 Web UI

没有单独的 UI 开关——`python -m noeta.agent` 始终提供附带的 Web 应用。启动离线运行器并在浏览器中打开聊天编辑器：

```bash
NOETA_AGENT_PROVIDER=stub python -m noeta.agent
```

运行器绑定 `NOETA_AGENT_HOST`（默认 `127.0.0.1`）和 `NOETA_AGENT_PORT`（`0` ⇒ 操作系统分配的临时端口），然后提供 Web 应用；访问 `<url>/chat` 编写目标，从会话中打开 `<url>/trace?task={id}` 检查其事件流和上下文。页面订阅 SSE 端点 `GET /stream?task=<id>`。服务器阻塞直到 `Ctrl-C`（SIGINT/SIGTERM）——没有宽限期开关。（旧的 `noeta run --serve --ui` 控制台标志在 TL6 中已移除。）

## 编码代理

`python -m noeta.agent` *就是* 工作区范围的编码代理：它读取、编辑、运行测试，并记录每一步。通过环境变量将其指向一个目录并启动服务器（`noeta code --workspace …` CLI 形式在 TL6 中已移除——工作区现在是 `NOETA_AGENT_WORKSPACE`）：

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-4o-mini \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
NOETA_AGENT_SQLITE=./session.sqlite \
python -m noeta.agent
```

然后通过 `<url>/chat` 的 Web 聊天编辑器驱动代理，或通过 HTTP：`POST /tasks`（body：`goal` + `agent` + 可选的 model *选择器*——provider/凭据从不从 body 读取，主机配置是权威的）。`agent` 字段选择一个命名代理（例如 `main` 通用配置）；技能是该代理接线的一部分，而非每次调用的标志。Shell 仍然通过狭窄的仅 argv 允许列表（pytest / git status&diff / npm-pnpm test）。

一旦会话被记录（将 `NOETA_AGENT_SQLITE` 设置为文件而非内存默认值），你可以通过 HTTP 接口管理它而无需重新运行代理（`noeta code list/inspect/tail/resume/…` 子操作在 TL6 中已移除）：

* `GET /tasks` —— 会话列表；`GET /stream?task=<id>` —— 实时 SSE 事件流（只读视图）
* `POST /tasks` —— 创建任务（`goal` + `agent`）；`POST /tasks/{id}/messages` —— 向现有任务追加后续目标
* `POST /tasks/{id}/approve` / `POST /tasks/{id}/deny` —— 批准或拒绝门控工具调用；`POST /tasks/{id}/answer` —— 回答模型提出的问题
* `POST /tasks/{id}/close` / `POST /tasks/{id}/reopen` / `POST /tasks/{id}/cancel` —— 关闭、重新打开或取消（生命周期）；`DELETE /tasks/{id}` —— 硬删除

对于无需服务器的只读检查，你也可以在进程内调用 `noeta.core.fold.fold(event_log, content_store, task_id)`。完整参考请参阅[`docs/noeta-agent.md`](noeta-agent.md)（工具、预设、技能、写入/shell 策略、HTTP 接口）。

## 接下来

* [核心概念](concepts.md) —— Task / EventLog / Engine 背后的模型
* [Noeta 代理](noeta-agent.md) —— `python -m noeta.agent` 工作区范围的编码代理（工具、预设、技能、HTTP 接口）
* [故障模式](failure-modes.md) —— 常见故障及恢复方法
* [守护进程 / Worker 循环](daemon.md) —— 常驻排空循环，现为库原语 `noeta.runtime.worker.WorkerLoop`（`noeta serve` 守护进程在 TL6 中已移除）
