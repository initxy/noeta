# 命令在哪里执行

工具读写的每个文件、跑的每条命令，都经过任务的**执行环境**（`ExecEnv`，`noeta/core/execenv.py`）。这一页讲有哪几种执行环境、命令沙箱管什么不管什么、怎么选、各自怎么配。

## 执行环境

工具自己从不调用 `open()` 或 `subprocess`，而是找任务的执行环境：`run(argv, cwd=…, env=…, timeout=…)`、`read_bytes`、`read_range`、`write_bytes`、`stat`、`realpath` 等。每个任务用哪个执行环境，由 `Runtime` 的参数 `env_for(state)` 决定。`noeta` 命令给的 `env_for` 让每个任务在自己的 `cwd` 上用本机环境（`noeta/cli/config.py` 的 `local_env_for`），设置里开了沙箱就带上沙箱。

<Diagram name="guide2-execenvs" caption="工具只认一个接口；env_for 按任务选出具体实现。" />

既然一切都经过执行环境，任务自己的文件也写在那里：给模型存的长工具输出（长输出目录）、计划文件、自动记忆、子代理的对话记录，都通过它写。它们跟着任务的工具走，而不是留在碰巧跑了某一步的那个工人上。

一共三种：

| 执行环境 | 入口名（`noeta.execenvs`） | 命令在哪儿跑 | 谁用 |
|---|---|---|---|
| `LocalExecEnv` | `local` | 本机子进程，用你的用户身份 | 默认，到处都是它 |
| `LocalExecEnv` 带 `LocalSandbox` | `local`（传 `sandbox=`） | `Bash` 命令关进 bubblewrap（Linux）或 Seatbelt（macOS）；文件工具同上 | 设置里 `sandbox.enabled` 打开时 |
| `ContainerExecEnv` | `container` | 每次操作一条 `docker exec`，进一个事先起好的容器 | 宿主代码通过 `Runtime(env_for=…)` 接入 |

第三方执行环境就是 `noeta.execenvs` 入口组里的一个类，按 `factory(cwd, **kw)` 构造（`noeta/core/registry.py` 的 `execenv`）。

## 本机环境

`LocalExecEnv`（`noeta/backends/execenvs/local.py`）让每条命令自成一个进程组，标准输入是关着的。超时或被取消时，整组先收 SIGTERM，5 秒后还没退就 SIGKILL，命令丢到后台的孙进程也一起收掉。输出边读边限长。

命令能做你这个用户能做的任何事。它继承你的环境变量，**但去掉 noeta 自己的凭据**（`noeta/toolkit/local_env.py`）：`NOETA_API_KEY`、`NOETA_STORE`、`NOETA_SERVER_TOKEN`、`NOETA_MCP_CLIENT_SECRET`、`NOETA_WEB_SEARCH_API_KEY`；存储是 Postgres 时还有 libpq 的变量（`PGPASSWORD` 等）；以及 noeta 配置里 `{env:NAME}` 引用到的每个变量。hooks 和 stdio 方式的 MCP 服务器也一样。

不开沙箱时，管住命令的是权限这一层：哪些命令要你点头、拒绝规则、哪些受保护路径一定要问。详见[权限与 hooks](./permissions.md)。其中有两条 shell 检查和这里相关：

- **拒绝规则也管命令。** `Read(...)` 拒绝规则同样拒掉读到匹配路径的 `Bash` 命令（`cat`、`<` 重定向等）；`Edit(...)` 拒绝规则拒掉写到匹配路径、或者删除/移动包含它的目录的命令。命令按宽松的方式解析，子 shell、heredoc、命令替换都看；通配符展开、软链接解析都通过执行环境完成（`noeta/plugins/permissions/deny_paths.py`、`rule_check.py`）。任何模式都绕不过去，`bypassPermissions` 也不行。
- **noeta 自己的配置目录。** 命令里出现 `.noeta` 就要问，只有 `bypassPermissions` 模式不问。

这些检查看的是命令文本。它们能挡住失误，挡不住故意藏起要碰什么的命令。要挡那种，得用沙箱或容器。

## 沙箱

沙箱就是 Claude Code 的 `sandbox` 设置，Linux 上用 bubblewrap、macOS 上用 Seatbelt（`sandbox-exec`）实现（`noeta/backends/execenvs/sandbox.py`、`noeta/toolkit/sandbox.py`）。它关住的是 **`Bash`**：前台命令、后台命令和 `Monitor` 脚本都算。文件工具（`Read`、`Edit`、`Write`、`Grep`、`Glob`）不在沙箱里跑，由权限规则管；`Edit(...)` 允许规则、`Read(...)` 拒绝规则和 `WebFetch(domain:…)` 允许规则也会并进沙箱自己的名单，两边口径一致。

<Diagram name="guide2-sandbox" caption="沙箱里的命令能写哪里、读不到哪里、能连到哪里。" />

沙箱里的命令能做的事：

- **读**：什么都能读，除了家目录里的凭据（`~/.ssh`、`~/.gnupg`、`~/.aws`、`~/.azure`、`~/.config/gcloud`、`~/.kube`、`~/.docker/config.json`、`~/.config/gh`、`~/.netrc`、`~/.git-credentials`、`~/.pypirc`、`~/.npmrc`、cargo 凭据、`~/.pgpass`、`~/.password-store`）、noeta 自己的家目录（`~/.noeta`，里面有访问令牌和任务存储；其中存下来的工具输出、项目记忆和粘贴的图片仍然能读）、`filesystem.denyRead`、`Read(...)` 拒绝规则和 `credentials.files` 里列的文件。`filesystem.allowRead` 可以在这些里面重新放开某个路径。
- **写**：只能写工作目录、`filesystem.allowWrite`、`Edit(...)` 允许规则和 `additionalDirectories` 里的路径，以及一个私有的 `$TMPDIR`。`filesystem.denyWrite` 和**受保护路径**永远写不了。
- **联网**：只能经过 noeta 在沙箱外跑的过滤代理，只能连 `network.allowedDomains` 里的主机（`*.example.com` 覆盖所有子域名，`host:port` 只开一个端口；`deniedDomains` 优先）。一个都没列，就哪儿都连不上。被拒的主机会附在命令输出后面的 `<sandbox_violations>` 里。允许的域名如果解析到回环、链路本地或云厂商元数据地址，同样拒绝。设了 `network.httpProxyPort` / `socksProxyPort` 就改走你自己的代理。
- **环境变量**：`credentials.envVars` 里列的会被清掉。

在 Linux 上，命令还有自己独立的网络、PID 和挂载命名空间，通过转发连到代理，另有 seccomp 过滤挡住 Unix socket 和新建用户命名空间。

### 受保护路径

项目里有些路径，写进去的东西会在之后被执行，而且是在沙箱外、没人确认的情况下：git 的 hooks 和配置、`.noeta/`（设置、hooks、插件）、`.vscode`、`.husky`、shell 启动文件、`.npmrc`、`.yarnrc`、`.cargo`、`.mvn`、`.bazelrc`、`.pre-commit-config.yaml` 等等（`noeta/toolkit/protected_paths.py`、`noeta/backends/execenvs/sandbox_plan.py`）。命令只要写了其中一个，你下次普通地 `git commit` 或 `npm install` 时，它就逃出了沙箱。

所以在沙箱里，这些路径是只读的；其中那些不经确认就会被执行的，如果还不存在，就在命令运行期间用一个空的只读占位文件顶上，让它也建不出来。它们上层的目录也不能被改名挪走。工作目录往下三层内的同名路径也会找出来。理由和否掉的其他方案见 [ADR-008](https://github.com/initxy/noeta/blob/main/docs/adr/008-sandbox-protected-path-placeholders.md)。

你会碰到的后果：在不是仓库的目录里 `git init` 会失败；命令写不了 `.noeta/` 下的任何东西（文件工具在权限规则允许时仍能改 `.noeta/skills` 和 `.noeta/commands`）。工作目录就是仓库根目录时，占位文件对 git 是隐藏的。

### 打开沙箱

写在任意一个设置文件里（`~/.noeta/settings.json`、已信任项目的 `.noeta/settings.json` 或 `.noeta/settings.local.json`，或者机器统一下发的 `managed-settings.json`，Linux 上在 `/etc/noeta`）：

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "allowWrite": ["~/.cache/pip"],
      "denyRead": ["~/secrets"]
    },
    "network": {
      "allowedDomains": ["pypi.org", "*.pythonhosted.org", "github.com"]
    },
    "excludedCommands": ["docker *"]
  }
}
```

Linux 上先装 bubblewrap（`apt install bubblewrap` 或 `dnf install bubblewrap`）。开了沙箱却起不来时，noeta 启动时会说明原因，命令照常不带沙箱跑；除非设了 `failIfUnavailable: true`，那样命令会被拒绝。

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `false` | 把 `Bash` 关进沙箱 |
| `failIfUnavailable` | `false` | 沙箱起不来时拒绝执行命令 |
| `autoAllowBashIfSandboxed` | `true` | 在沙箱里跑的命令不用审批（计划模式除外） |
| `allowUnsandboxedCommands` | `true` | 调用可以设 `dangerouslyDisableSandbox: true` 出沙箱跑。不管允许规则怎么写，这一步都要审批：`auto` 下由分类器判断，`bypassPermissions` 下直接跑，`dontAsk` 下直接拒绝（关掉后这个参数不起作用，命令照样在沙箱里跑） |
| `excludedCommands` | `[]` | 这些命令模式（`docker *`、`Bash(npm *)`）总是在沙箱外跑 |
| `filesystem.allowWrite` / `denyWrite` / `denyRead` / `allowRead` | `[]` | 上面说的路径名单 |
| `filesystem.disabled` | `false` | 只管网络，不管文件 |
| `network.allowedDomains` / `deniedDomains` | `[]` | 上面说的主机名单 |
| `network.allowUnixSockets`、`allowAllUnixSockets`、`allowLocalBinding` | 关 | Unix socket 和绑定本地端口（macOS；`allowAllUnixSockets` 在 Linux 上也会放开对 Unix socket 的拦截） |
| `network.httpProxyPort` / `socksProxyPort` | 无 | 改走你自己的代理 |
| `credentials.files` / `credentials.envVars` | `[]` | `mode: "deny"`（`"mask"` 也按 deny 处理）的条目会藏起文件或清掉变量 |
| `enableWeakerNestedSandbox` | `false` | Linux：不挂新的 `/proc`，给挂不了的宿主用 |
| `allowNestedUserNamespaces` | `false` | Linux：允许命令自己建用户命名空间 |
| `bwrapPath` | 从 `PATH` 找 | 指定一个绝对路径的 `bwrap`；只认机器统一下发的设置 |

名单类的键在多个文件之间累加。项目自己的设置文件只能把沙箱**收紧**：可以打开 `enabled`、`failIfUnavailable`，可以关掉 `allowUnsandboxedCommands`、`autoAllowBashIfSandboxed`，反过来不行；会放宽沙箱的键和名单（`excludedCommands`、`allowWrite`、`allowRead`、`allowedDomains`、socket 和代理相关的键、嵌套沙箱开关、`filesystem.disabled`）在项目文件里一律忽略并给出警告（`noeta/cli/settings_files.py` 的 `merge_sandbox`）。

沙箱允许什么，会通过一条 `sandbox_instructions` 说明告诉模型；有变化就重发，沙箱关了也会告诉它。

## 容器环境

`ContainerExecEnv`（`noeta/backends/execenvs/container.py`）把每次操作都变成一条进运行中容器的 `docker exec`：命令如此，文件读写也如此（用 `cat`、`stat`、`realpath`，写文件先写临时文件再改名）。命令在容器里自成一个进程组，超时时在容器里把它杀掉，而不只是杀掉本地的 `docker` 客户端。宿主机的环境变量传不进命令：你给的变量放在客户端的环境里传，不出现在命令行上。镜像里需要 `sh`、GNU coreutils、util-linux 的 `setsid` 和 `bash`（Debian、Ubuntu 系的镜像都有；busybox 和 Alpine 没有）。

起容器是宿主自己的事；`start_container`（`noeta/backends/execenvs/container_provision.py`）是个帮手函数，默认就比较严：不联网、根文件系统只读（`/tmp`、`/var/tmp`、`/run`、`/root` 可写）、只留少量 Linux capabilities、`no-new-privileges`、最多 1024 个进程，可选 `memory` / `cpus` 限额。传入带 `network.allowedDomains` 的 `sandbox` 设置时，容器会加入一个自己专用的内部网络，唯一的出口是本进程在该网络网关上跑的过滤代理（进程重启后用 `ensure_container_proxy` 再把代理跑起来）。文件相关的键在容器里没有对应：容器只看得到挂进去的东西。

`noeta` 命令没有哪个设置能切到容器环境，要在宿主代码里接：

```python
import asyncio
import noeta
from noeta.backends.execenvs.container import ContainerExecEnv, start_container

async def main() -> None:
    name = await start_container(
        "python:3.12-bookworm",
        mounts=[("/srv/checkouts/app", "/workspace")],
        sandbox={"enabled": True, "network": {"allowedDomains": ["pypi.org"]}},
    )
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")
    runtime = noeta.Runtime(
        store=store, agents=agents, provider_for=lambda _name: model,
        env_for=lambda state: ContainerExecEnv(name, cwd=str(state.config.get("cwd") or "/workspace")),
    )
    client = noeta.Client(store)
    await client.create("main", "Run the tests and fix what fails.", config={"cwd": "/workspace"})
    await runtime.run_until_idle()

asyncio.run(main())
```

`env_for` 对每个任务调用一次，所以可以给每个任务（或每一方使用者）一个单独的容器。这时任务的 `cwd` 是容器里的路径。

## 怎么选

| 场景 | 用什么 |
|---|---|
| 你自己的机器、你在旁边看着 | 本机环境；如果放手让命令不问就跑，打开沙箱 |
| 在自己机器上无人值守（`-p`、CI） | 本机环境加沙箱，`failIfUnavailable: true`，`allowedDomains` 列得越短越好 |
| 多台机器上的多个工人 | 每个工人在同一路径挂同一个共享文件系统，或者用所有工人都能连到的容器环境（见[作为服务端运行](./server.md)） |
| 不可信的代码，或互相不能看到的几方使用者 | 宿主代码里给每个任务或每一方单独一个容器 |

多台机器时，执行环境从每个工人看过去都得一样，因为任务每走一步都可能换个工人接着跑。用容器环境时，任务的文件都在容器里，别的不用共享（noeta 家目录的路径要在每个工人上一致，而且在容器里可写）。用本机环境加共享文件系统时，noeta 家目录里存长输出、计划和记忆的那几个目录也得在共享盘上。

不管哪种环境，有两样东西只在一个工人本地：后台 `Bash` 命令（命令跑着的时候，它的任务一直由那个工人占着，见[子代理与后台任务](./subagents.md#后台-shell-命令)），以及沙箱或带沙箱的容器用的过滤代理——它跑在启动它的那个进程里。

## 出问题时

- **`bubblewrap (bwrap) is not installed`**、**`bwrap cannot run: …`**，或者 **`bwrap cannot create the sandbox here: …`**（系统不让建用户命名空间）：沙箱起不来。命令照常不带沙箱跑（启动时有提示），设了 `failIfUnavailable` 就带着这个原因被拒。macOS 上缺 `/usr/bin/sandbox-exec` 也一样；其他平台没有沙箱。
- **命令写某个点文件或 `.git/hooks` 失败。** 那是受保护路径；这一步你自己来做，或者经你同意在沙箱外跑。
- **沙箱里下载失败。** 看输出里的 `<sandbox_violations>`，把主机加进你自己设置里的 `network.allowedDomains`（项目设置加不了）。
- **找不到 `docker`，或容器没了。** `ContainerExecEnv` 抛 `ContainerError`，这次工具调用以这个错误失败，模型看得到。
- **命令跑到一半工人挂了。** 这条命令的结果算"不确定"，下一个工人把这一点告诉模型，而不是再跑一遍（[单写者与崩溃恢复](../how-it-works/recovery.md)）。
