# 用 Docker 部署

这一页在一台 Docker 主机上把 noeta 跑成一个小集群：一个 Postgres，一个提供 HTTP 接口的
`noeta serve`，再加几个分着跑任务的 `noeta worker` 容器。用到的文件都在
[`deploy/`](https://github.com/initxy/noeta/tree/main/deploy) 里。每条命令、每个参数是干什么的，
以及怎样把同一套部署铺到多台机器上，见[作为服务端运行](./server.md)。

## `deploy/` 里有什么

| 文件 | 是什么 |
|---|---|
| `Dockerfile` | 镜像：基于 `python:3.12-slim-bookworm`，用当前检出的源码构建 noeta，带 `postgres` 和 `server` 两个 extra |
| `compose.yaml` | Postgres 16、`serve`（只提供接口，`--no-worker`）和 `worker`（默认 3 个副本） |
| `.env.example` | `compose.yaml` 要读的配置：模型、密钥、访问令牌、Postgres 密码、工人数量、端口 |

我们不发布现成的镜像，要用就从你检出的源码自己构建。

## 镜像

```sh
docker build -f deploy/Dockerfile -t noeta .      # 在仓库根目录执行
docker run --rm noeta --version
```

- **跑什么。** 入口是 `noeta`，默认命令是 `worker`。想跑别的就把 `serve …` 或其他子命令接在后面。
- **装了什么。** 先按 `uv.lock` 里锁定的版本装依赖，再装当前源码打出来的 wheel。另外有 `git`、
  `ripgrep`（模型在 `Bash` 里搜代码常用 `rg`）、`bubblewrap`（沙箱，这里[默认不开](#命令和沙箱)）、
  `openssh-client`，以及负责回收命令遗留子进程的 `tini`。不带终端界面、网页版和 ACP。
- **要别的 extra。** `docker build --build-arg EXTRAS="postgres server acp" …`。
- **以谁的身份跑。** 用户 `noeta`（uid 1000；挂进来的工作目录属于别的用户时，用
  `--build-arg UID=… GID=…` 改成一致）。noeta 的主目录是 `/home/noeta/.noeta`（`NOETA_HOME`），
  工作目录是 `/workspace`。

## 把集群跑起来

```sh
cp deploy/.env.example deploy/.env
# 填上 NOETA_MODEL、NOETA_API_KEY（不是 Anthropic 的模型还要填 NOETA_PROVIDER / NOETA_BASE_URL）、
# NOETA_SERVER_TOKEN 和 POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d --build --wait
```

`deploy/.env` 和 `compose.yaml` 在同一个目录，compose 会自动读它。`--wait` 会等 Postgres 和
`serve` 都通过健康检查才返回（`serve` 的检查是带着令牌请求一次 `GET /agents`）。接口监听在
Docker 主机的 `127.0.0.1:8642`；想让别的机器访问，把 `NOETA_PORT` 设成 `0.0.0.0:8642`，并在
`compose.yaml` 里把别人访问用的主机名再加一个 `--allow-host`（`serve` 只认给定的 `Host` 名字，
现在是 `localhost`、`127.0.0.1` 和 `serve`）。

建一个任务，然后跟着看它的输出：

```sh
export NOETA_SERVER_TOKEN=…            # deploy/.env 里填的那个
H="Authorization: Bearer $NOETA_SERVER_TOKEN"
curl -s -X POST http://127.0.0.1:8642/tasks -H "$H" -H 'content-type: application/json' \
  -d '{"agent":"main","input":"列一下这里有哪些文件"}'
curl -sN http://127.0.0.1:8642/tasks/<任务 id>/watch -H "$H"
```

也可以在任何装了 noeta 的机器上用终端界面连过来：`noeta --server http://127.0.0.1:8642`。
所有接口见 [HTTP / WebSocket 接口](../reference/http-api.md)。

通过 HTTP 建的任务，请求里没写 `permission_mode` 时按 `auto` 模式开始；需要人拍板的事会挂在
`GET /tasks/{id}/pending` 里，等有人回答。`bypassPermissions` 一律拒绝，除非 `serve` 和工人启动时
都加了 `--allow-bypass-permissions`。

## 容器之间共享什么

所有 noeta 容器都把同样两个卷挂在同样的路径上，因为任务每走一步都可能换一个工人接着跑：

- **`workspace` 挂在 `/workspace`**：任务的工作目录。要让 agent 干活的仓库就放这里（用
  `docker compose cp` 拷进去、让任务自己 `git clone`，或者把这个命名卷换成主机目录的绑定挂载）。
- **`noeta-home` 挂在 `/home/noeta/.noeta`**：agent、插件、`config.toml`、settings 文件和 MCP
  服务器都在这里，所以每个工人用的是同一套；另外还有存下来的长工具输出、计划文件和自动记忆，
  任务换到哪个工人，这些东西都跟着过去。

任务本身存在 Postgres 的 `pgdata` 卷里。后台 `Bash` 作业和它的日志只留在启动它的那个工人里
（见[已知限制](../operations/limitations.md#多台机器)）。

这几个都是同一台 Docker 主机上的命名卷。要把工人分到几台主机上，得给它们一个在每台主机上都挂在
同样路径的共享文件系统（比如 NFS 卷），把 `NOETA_STORE` 指向大家都连得上的 Postgres，再在那边
跑同一个镜像。

## 扩容、停止和重启

```sh
docker compose -f deploy/compose.yaml up -d --scale worker=5   # 或者改 .env 里的 NOETA_WORKERS
docker compose -f deploy/compose.yaml stop worker              # 发 SIGTERM：手上的任务交回存储
docker compose -f deploy/compose.yaml logs -f worker
```

- **正常停掉的工人**（`docker stop`、`compose stop`、滚动重启）会马上把手上正在跑的任务交回存储，
  别的工人从日志接着跑。
- **被强杀的工人**（`docker kill`、主机断电）会一直占着它的任务，直到租约过期（30 秒，
  `worker --lease` 可调），然后别的工人认领。它死的时候正在跑的工具调用，会以「结果未知」告诉模型，
  除非这个工具可以安全地重跑（见[单写者与崩溃恢复](../how-it-works/recovery.md)）。
- **升级。** 不同版本的工人不能共用一个存储。拉下新源码后先
  `docker compose -f deploy/compose.yaml down`（卷会保留），再 `up -d --build --wait`；新版本第一个
  启动的进程会把存储的表结构升上去。

## 命令和沙箱

在这些容器里，模型跑的命令以 `noeta` 用户身份执行，不套沙箱：容器本身就是边界。命令能看到工作目录、
noeta 的主目录和容器能连的网络；但永远拿不到 noeta 自己的凭据（`NOETA_API_KEY`、`NOETA_STORE`、
`NOETA_SERVER_TOKEN` 不会传给任何命令）。权限规则和权限模式照常生效（见[权限与 hooks](./permissions.md)）。

Linux 沙箱（bubblewrap）需要用户命名空间和一个新挂载的 `/proc`，Docker 的默认安全配置都不让。
要在容器里用它，工人得同时加上下面三个选项，而每一个都会放松容器本身的隔离：

```yaml
  worker:
    security_opt:
      - seccomp=unconfined
      - apparmor=unconfined
      - systempaths=unconfined
```

然后在共享主目录的 `settings.json` 里打开它（`"sandbox": {"enabled": true,
"failIfUnavailable": true}`，见[命令在哪里执行](./sandbox.md)）。只加前两个的话沙箱起不来（bwrap
挂不上 `/proc`），`enableWeakerNestedSandbox` 怎么设都一样。要跑不可信的代码，更稳妥的是在宿主
代码里给每个任务配一个容器（`Runtime(env_for=…)`）。

## 网页版

镜像是直接拿源码、不装 node 构建的，所以不带发布包里那份[网页版](./front-ends.md#网页版)。要用的话，在主机上
构建出 `web/dist`（`cd web && npm ci && npm run build`），挂进 `serve`（`- ../web/dist:/web:ro`），再在它的命令里加上 `--web /web`。
