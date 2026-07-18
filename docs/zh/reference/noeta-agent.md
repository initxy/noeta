# noeta-agent 平台（`python -m noeta.agent`）

Noeta 的官方产品是一个**可部署的多用户 agent 服务**：FastAPI 后端加 React/TypeScript SPA，作为单个进程交付，并为每个会话（session）供给一个沙箱容器用于 agent 执行。用户登录后在**空间**（space）中协作、与 agent 进行**会话**；agent 的 skill、知识、记忆、MCP 连接器和配置都以空间为作用域。起统领作用的决策见 [server-platform ADR](https://github.com/initxy/noeta/blob/main/docs/adr/server-platform-product.md)。

## 启动

**唯一**入口是 `python -m noeta.agent` —— 零参数，所有配置都通过 `apps/noeta-agent/.env` 和环境变量完成（见[配置](configuration.md)）。它在同一个端口（默认 8000）上同时提供 `/api/v1/*` 下的 REST + SSE API 和 `apps/web/dist` 里构建好的 SPA。从一份 checkout 出发，Makefile 封装了常用流程：

```bash
make install   # 首次：uv sync + 前端依赖
make run       # 构建 SPA + python -m noeta.agent   → http://127.0.0.1:8000
make dev       # 热重载：后端在 8000 + vite dev server 在 5273（走代理）
```

### 启动模式

- **零凭证（默认）。** 一切留空：确定性 **mock provider**（一段脚本化的 FakeLLM 演示 —— 提问、skill 激活、写回的回答）、**dev-login**（任意用户名）、SQLite 存储、沙箱关闭。完全离线；测试套件和 CI 跑的正是这个模式。
- **真实网关。** 把 `LLM_BASE_URL` + `LLM_API_KEY` 设置为任意 OpenAI-Responses 兼容网关（`/responses` 会被自动追加）；在 `models.json` 里定义模型菜单；可选地再加一个第二网关做按模型路由。参见[接入 OpenAI 兼容网关](../how-to/configure-provider.md)。
- **打开沙箱。** `SANDBOX_ENABLED=true` + 本地 Docker daemon + 现成的 [AIO Sandbox 镜像](https://github.com/agent-infra/sandbox)。每个会话获得自己的容器，带实时的 Browser / Terminal / Code 预览面板。

## 架构

一个**模块化单体**：单进程、单部署单元，缝（seam）是接口而不是服务。

```text
apps/web (React SPA)  ──  /api/v1 REST + 每会话一条 SSE
        │
noeta.agent.api        各 router（auth、sessions、spaces、skills、knowledge、
        │              mcp、templates、memories、feedback、channels、admin）
noeta.agent.auth       AuthProvider 缝（dev-login 参考实现）
noeta.agent.host       引擎宿主：AgentService（内嵌 noeta.sdk Client + worker 池）、
        │              envelope→UI translator、provider 装配、Docker 沙箱 provider
noeta.agent.store      应用 SQLite（用户、空间、会话、…）
noeta.agent.services   知识同步/解析、频道、反馈分析
        │
     noeta.sdk         进入引擎的唯一通道
```

关键结构决策：

- **会话与空间只是应用层的索引。** 一个会话聚合一个或多个引擎任务（工作流会话的每个节点各拥有一个根任务），并拥有一个工作区目录和一个沙箱。应用层之下，引擎只认识 Task；每一次状态变更都经 `noeta.sdk` 的 `Client` 动词流动，EventLog 始终是唯一事实来源。
- **wire 上是翻译后的事件，不是原始事件。** 后端用一个确定性、无状态的纯函数（`noeta/agent/host/translator.py`）把规范的引擎事件翻译成一套扁平的 UI 事件词汇表，经**每会话一条的 SSE 流**下发。Replay 是从 EventLog 出发、经 `since_seq` 游标的**重新推导** —— 不存在可能漂移的持久化 UI 投影。Token delta 以临时帧的形式随流而下，从不持久化、从不 replay。原始 envelope 只在管理员 trace 面上提供。完整词汇表见 [HTTP API 参考](http-api.md)。
- **执行只在沙箱里。** agent 的 shell 与文件副作用只发生在每会话专属的容器内；宿主机不暴露任何 shell 工具，也**没有逐调用的审批流程**（带审批的宿主机执行是单用户产品的便利；在共享服务器上它是一个提权面）。会话工作区以读写方式挂载；空间的知识与 skill 以只读方式挂载。没有 Docker 时，平台降级为纯对话模式，shell 执行关闭。
- **认证是一条缝，不是一个功能。** 每个请求都经 `AuthProvider` 接口认证（`noeta/agent/auth/provider.py`）；开源发行版自带 `DevLoginProvider`（任意用户名、签名会话 cookie），并为 OIDC/SSO 留着这条缝。核心里不内置任何厂商的身份系统。

## 会话 / 空间模型

- 每个用户都有一个**个人空间**；**团队空间**的成员由所有者管理（角色：owner / member）。会话可见性 = 空间成员身份。
- 空间为 agent 的工作材料划定作用域：**skill**（内置 + 空间上传）、**知识源**（`git_repo` / `local_dir` 同步）、**长期记忆**、**MCP 连接器**（每空间的别名与工具子集，每轮解析进宿主）、**agent-config**（人设 prompt、默认模型 / 推理力度、知识选择、记忆开关），以及**模板 / 工作流模板**。
- 会话可以是普通对话、从模板启动，或是**多节点工作流会话** —— 每个节点是各自独立的根任务，经由一份生成后由用户确认的交接文档向前推进。
- **反馈闭环**把成员评分变成由所有者把关的建议（采纳进记忆、应用 skill 补丁，或导出为 markdown 报告）。

## 管理员控制台

管理员是一个**角色，不是一套独立部署**：列在 `ADMIN_USERS` 里的用户名在同一台服务器上获得控制台（其他人访问 `/api/v1/admin/*` 一律 404）。它提供用量统计（用户 / 空间 / 会话 / skill / 知识）、跨空间列表与逐空间下钻、内置 skill 管理、动态配置（例如热切换 dev-login），以及**原始事件 trace** —— 任意会话未经翻译的 envelope 流，在 trace UI 中于客户端折叠展示。trace 面是诊断工具，有意与产品 wire 分开。

## 诚实的边界（v1）

- **单进程、单实例** —— 应用状态是 SQLite；水平扩展是后续工作。
- **默认认证是 dev-login**；真实部署必须接入身份提供方。
- **尚无限流与配额。**
- **沙箱隔离是「进程 + 挂载 FS」**，不是完整牢笼。

## 部署

平台裸跑就能用：`uv` + 一个可写的 `DATA_DIR` +（可选）供沙箱使用的 Docker。[`examples/deployment/`](https://github.com/initxy/noeta/tree/main/examples/deployment) 提供可选的 docker-compose 封装（应用容器 + 沙箱访问 + 数据卷）。

## 另见

- [HTTP API 参考](http-api.md) —— 每条路由与 SSE 词汇表
- [配置](configuration.md) —— 每个 `.env` 键与默认值
- [操作指南：使用平台](../how-to/use-the-coding-agent.md) —— UI 走查
- [接入 OpenAI 兼容网关](../how-to/configure-provider.md)
