# 快速开始：5 分钟看到 Noeta 跑起来

**你将完成：** 零凭证启动平台，登录，进行一段脚本化的对话，并看着它从事件日志 replay 出来。**不需要 API key、不需要 Docker、不需要注册账号** —— 默认的 mock provider 是一个确定性的 LLM 替身。

## 1. 安装

需要 Python 3.11+（配 [uv](https://docs.astral.sh/uv/)）和 Node 20+：

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install        # uv sync + 前端依赖
```

## 2. 启动平台

```bash
make run            # 构建 SPA + python -m noeta.agent
```

这会在 <http://127.0.0.1:8000> 启动服务器 —— 离线 mock LLM、dev-login、SQLite 存储、沙箱关闭（即零凭证模式；每个配置键都是可选的）。底层入口始终是 `python -m noeta.agent`，只通过环境变量配置，没有命令行参数。Ctrl-C 即可停止。

## 3. 登录并对话

打开该 URL，用**任意用户名**登录（dev-login —— 开发用的认证 provider）。你会落在自己的个人空间（space）里。发起一个会话（session），发送一条消息 —— 例如：

```text
Write me a short report on the state of the project.
```

mock provider 会用一段脚本化的演示把*真实*机制完整走一遍：agent 向你提出一个澄清问题（回答它）、激活一个 skill，然后把回答写回来。其中每个时刻 —— 那次提问、那次 skill 激活、每个轮次边界 —— 都是一条被记录的事件。

## 4. 看看底下的日志

对话进行到一半时刷新页面：流会分毫不差地重建，因为 UI 的 replay 靠的是**从事件日志重新推导**，而不是信任内存里的任何东西。想看原始记录，把你的用户名加进 `apps/noeta-agent/.env` 的 `ADMIN_USERS`，重启，然后打开管理员控制台的 **Trace** 视图 —— 那里是任意会话未经翻译的引擎事件（LLM 轮次、工具调用、token/cache 统计）。这为什么重要，参见[事件溯源](../concepts/event-sourcing.md)。

## 5. 在进程内驱动（可选）

如果你更喜欢代码而不是浏览器，同一个应用几行代码就能组装出来（要对外提供服务，只差一个 `uvicorn.run`）：

<!-- runnable: smoke -->
```python
from noeta.agent.main import create_app

# Fully offline defaults: the deterministic mock LLM, SQLite app storage,
# dev-login. create_app assembles the FastAPI application without serving it.
app = create_app()
assert "/api/v1/health" in app.openapi()["paths"]
print("application assembled")
```

## 下一步

- **接入真实模型** —— [配置 provider](../how-to/configure-provider.md)：任意 OpenAI-Responses 兼容网关，两行 `.env` 即可。
- **打开沙箱** —— `SANDBOX_ENABLED=true` + Docker 让每个会话拥有自己的容器，带实时的 Browser / Terminal / Code 面板；见[使用平台](../how-to/use-the-coding-agent.md)。
- **构建你自己的 agent** —— [你的第一个 agent](first-agent.md) 是一个 20 分钟的 SDK 引导教程，用到 `@tool`、`Options` 和 `query()`。
- **理解设计** —— 从[事件溯源](../concepts/event-sourcing.md)和[架构概览](../architecture/overview.md)开始。
