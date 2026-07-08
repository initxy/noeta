# 快速开始：5 分钟内让 Noeta 跑起来

**你将完成以下操作：** 安装 Noeta、启动离线编程代理、打开 Web UI、发送消息，并查看 trace。**无需 API 密钥** —— 默认的 `stub` provider 是一个确定性的 LLM 替身。

## 1. 安装

```bash
pip install noeta-agent
```

这会同时安装 SDK 和 runtime。需要 Python 3.11+。

## 2. 启动代理

```bash
python -m noeta.agent
```

这会启动编程代理服务器，使用离线 `stub` provider 和内存存储。它会打印一个 URL —— 类似 `http://127.0.0.1:54321/`。服务器会阻塞运行直到按 Ctrl-C；没有守护进程模式。

## 3. 打开 Web UI

在浏览器中打开打印出的 URL，然后导航到 `/chat`。你应该能看到聊天输入框。输入一条消息 —— 例如：

```
List the Python files in this directory and tell me what each one does.
```

`stub` provider 返回预设的两轮响应，所以你会看到代理"思考"，然后用预设好的内容回复。重点不在于回复的质量 —— 而在于每一步都被记录下来了。

## 4. 查看 trace

在聊天视图中，点击某个会话以打开它的 trace。trace 视图展示了该会话 EventLog 中的每一个事件：用户消息、LLM 轮次、所有工具调用、工具结果以及最终回复。每一行都包含 token 计数和 cache 统计。

这个 trace 不是从进程内存中生成的 —— 它是从 EventLog 中 fold 出来的，和恢复代理自身状态的方式完全一样。参见 [事件溯源](../concepts/event-sourcing.md) 了解为什么这很重要。

## 5. 在进程内驱动（可选）

如果你更喜欢用代码而不是浏览器，只需几行代码就能启动同样的后端：

<!-- runnable: smoke -->
```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# 默认配置完全离线：stub provider、:memory: 存储。
# port=0 绑定由操作系统分配的端口。工作区为当前目录。
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
    print(f"Backend running at {url}")
finally:
    shutdown()
```

## 下一步

- **接入真实模型** —— [配置 provider](../how-to/configure-provider.md) 介绍了 Anthropic 和兼容 OpenAI 的配置方式。
- **构建你自己的代理** —— [你的第一个代理](first-agent.md) 是一个 20 分钟的 SDK 引导教程，涵盖 `@tool`、`Options` 和 `query()`。
- **理解设计** —— 从 [事件溯源](../concepts/event-sourcing.md) 和 [架构概览](../architecture/overview.md) 开始。
- **用编程代理做实际工作** —— [使用编程代理](../how-to/use-the-coding-agent.md) 涵盖了工作区设置、预设和技能。
