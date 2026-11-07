# 网页、桌面与编辑器

除了[终端界面](./terminal.md)，noeta 还有网页版（`web/`）、桌面版（`desktop/`），以及给编辑器用的 Agent Client Protocol 接入（`noeta acp`）。本页讲每种怎么跑，以及怎样把它们连到服务器上。

## 底下只有一套接口

每个前端都只是同一套客户端接口（`noeta/core/client.py`：create、send、answer、cancel、read、watch）的翻译。前端都不保存任务状态，显示的内容全部从任务日志读出来，所以同一个任务可以先在终端里打开，再到浏览器里，再到编辑器里，每处看到的都是完整对话。

<Diagram name="guide-ways-in" caption="所有入口最后都落到同一套客户端接口上，背后是同一个存储和同一批工人" />

| 前端 | 连的是 | 代码 |
|---|---|---|
| 终端界面 | `AppAPI`：本进程里的 `LocalApp`，或加 `--server` 时的 `RemoteApp` | `tui/`、`noeta/host/app.py`、`noeta/host/remote.py` |
| `noeta -p`、Agent SDK | Claude Code 的 stream-json 协议，本进程内 | `noeta/cli/headless.py`、`noeta/host/stream_json.py` |
| 网页版 | `noeta serve` 的 HTTP 接口 | `web/src/api/client.ts`、`noeta/host/http.py` |
| 桌面版 | 网页版，连它自己启动的 `noeta serve` | `desktop/src/server.ts` |
| 编辑器（Zed、JetBrains、Neovim 等） | stdio 上的 ACP，本进程内或 `--server` | `noeta/host/acp.py` |
| 你自己的程序 | `noeta.Client` / `noeta.LocalApp`，或 `noeta.RemoteClient` | [Python 接口](../reference/python-api.md) |

要接入新的外部协议，就在 `noeta/host` 里再加一层翻译，绝不另起一套接口。

## 网页版

**还在实验阶段。** 网页版和桌面版都要从源码构建：没有发布（没有 npm 包，Release 里也没有安装包），CI 不跑它们的测试（本地用 `make frontends` 跑），桌面版的安装包也没有签名。正式支持的入口是终端界面、`noeta -p`、ACP 和 HTTP 接口。

网页版是一个 React 页面，调用 `noeta serve` 的 HTTP 接口（和 `RemoteClient` 调的是同一批）。它有按目录分组的对话列表、流式显示的 markdown 对话、工具卡片（Bash 输出、改文件显示成 diff、子代理嵌套显示）、和 Claude Code 选项一致的审批卡片、`AskUserQuestion` 表单、计划审阅、任务清单面板、斜杠命令菜单、从任务目录补全的 `@` 引用、粘贴和拖入图片、模式和模型选择、用量和上下文占用、回退、重命名、归档和搜索、亮暗两种主题，以及手机尺寸的布局（`web/screenshots/README.md` 逐项配了截图）。

先构建，再让服务器顺带提供它：

```bash
cd web && npm install && npm run build      # 输出到 web/dist
noeta serve --web web/dist                  # 需要 server 这个 extra
```

`noeta serve` 第一次生成令牌时会打印出来；打开它打印的地址（默认 `http://127.0.0.1:8642/`），页面会要你输入一次令牌。也可以直接放在地址里：`http://127.0.0.1:8642/#token=…`（`#` 后面的部分不会发给服务器；页面把令牌存进浏览器本地存储，并从地址栏里去掉）。点"Disconnect"会忘掉它。

页面文件本身不需要令牌就能取到（里面不含令牌），并带一条内容安全策略，只允许页面和自己的服务器通信。如果页面放在别的地方，只有你点名的来源才能调接口：`noeta serve --allow-origin https://ui.example.com`。

要改页面本身，在 `web/` 里运行 `npm run dev`：它启动 Vite，把接口请求转发给一个正在运行的 `noeta serve`（`NOETA_SERVER`，默认 `http://127.0.0.1:8642`）。

没有的东西：云端环境、仓库和分支选择（noeta 就在服务器的目录里跑）、终端面板和文件树。对话不能删除，因为存储会保留每个任务的日志；想让它不出现在列表里就归档。

## 桌面版

和网页版一样**还在实验阶段**：要从源码构建，安装包没有签名。

桌面版就是装进 Electron 窗口的网页版，后面连一个它自己启动、自己关闭的本地 `noeta serve`。不用连接、不用粘贴任何东西：启动时它找到 `noeta`，挑一个空闲的本机端口，为这次运行生成一个新令牌，然后启动

```text
noeta serve --host 127.0.0.1 --port <空闲端口> --web <构建好的网页版> [--allow-root DIR …]
```

令牌放在服务器进程的环境变量里（`NOETA_SERVER_TOKEN`），从不出现在命令行上（`desktop/src/server.ts`）。退出应用时服务器随之停止。

```bash
cd desktop && npm install
npm run web          # 先构建 ../web
npm start            # 构建并打开应用
npm run dist         # 安装包：macOS dmg、Linux AppImage 和 deb（未签名）
```

它在 `PATH` 以及 `~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 里找 `noeta`（从程序坞启动的应用拿不到登录 shell 的 `PATH`）；要指定别的位置，设 `NOETA_DESKTOP_NOETA`，或在应用的 `settings.json`（Settings，`⌘,`）里写 `noetaPath`。找不到时窗口里会显示安装命令，那条命令带着 `server` extra。File > Open Project Folder（`⌘O`）选新对话的工作目录。服务器在你的主目录里运行，所以主目录下的文件夹直接就能用；主目录以外的文件夹会加成服务器的 `--allow-root`，服务器因此要重启（有对话正在跑或在等你时会先问一句；`desktop/src/main.ts`）。本地服务器挂了，窗口会显示它最后的输出，View > Reload 重新启动它。窗口在后台时，某个对话需要你、或者跑完了，会弹系统通知。

桌面版总是跑自己的本地服务器；要连远端服务器，打开那台服务器提供的网页版。

## 网页版和桌面版里的插件

点页头的插件按钮（或输入 `/plugins`）打开插件对话框。**Installed** 列出代理用到的插件、每个带哪几部分、哪里有问题：可以只在这个会话里关掉某个插件（从下一轮起生效，走 `config` 信号的 `plugins_off`），也可以让之后新建的会话都不带它（本机的默认值），或者把它删掉（哪些插件能开关见[关掉插件](../reference/plugins.md#switching-a-plugin-off)）。**Discover** 列出已配置的插件市场里的插件，先把来源、commit 和包含哪几部分摆出来，你确认了才安装；也可以直接填一个路径，或者钉了 commit 的仓库地址。通过 HTTP 安装和删除只在两种服务器上能用：只监听本机地址的（桌面版就是），或者启动时加了 `--allow-plugin-install` 的；别的情况对话框会说明原因。

插件不写前端代码也能显示的东西（`Extension.ui`）出现在输入框附近：状态项在状态行里，小部件在输入框上方或下方，提示可以关掉。会话日志一有新内容，页面稍等片刻就重新取一次。

带网页部分的插件（见[写一个插件 › 网页部分](./plugins.md#网页部分)）只在当前打开的会话用到它时才在页面里运行：页面从服务器导入它的模块，调用 `activate(ctx)`；插件被关掉、或者你离开这个会话，它加进页面的东西全部撤掉。它的组件放进页面预留的位置：`statusbar`（输入框下面那一行，排在模型选择之后；`order` 为负数的排到它前面）、`sidebar`（对话旁边那一栏）、`settings`（对话框的 Settings 页）、`session.header`（会话标题）、`composer`、`tool:<Tool>`（工具卡片的正文）、`request:<Tool>`（审批卡片或提问卡片上，关于这次调用要展示的内容）、`record:<plugin>/<kind>`（对话里的一条记录）、`codeblock:<language>`（渲染后的 markdown 里的代码块）和 `theme`。某个组件抛了异常，原地只显示插件名和错误，页面其余部分照常；网页部分加载失败、或者一直没等到另一个插件的位置，对话框里会点名是哪个插件。在桌面版里，`ctx.desktop` 就是桌面版给页面的那几个接口（`project`、`chooseProject`、`notify`）；在浏览器里它是 `null`。

内置插件的界面也走这条路，各自的网页部分放在页面源码的 `web/src/plugins/<name>/` 下：待办列表（`todos`）、权限模式选择和 Shift+Tab 切换（`permissions`）、待审阅的计划（`plan_mode`）、自动压缩前的倒数（`compaction`）、会话标题（`title`）、命令和文件工具的卡片及 diff（`shell`、`fs`）、子代理和 workflow 的进度（`subtasks`、`workflow`）、代码审查的发现（`findings`），以及 Settings 页里记忆、MCP、hooks 三块。某个会话关掉其中一个，它的界面跟着消失；没有插件接管的工具显示成普通卡片（输入和输出）。审批和提问、回退（rewind）、对话本身和模型选择由页面自己负责。

## 编辑器：`noeta acp`

`noeta acp` 在 stdin 和 stdout 上提供 [Agent Client Protocol](https://agentclientprotocol.com)，Zed、JetBrains 系列 IDE、Neovim 等 ACP 客户端都能把 noeta 当作自己的代理。需要 `acp` 这个 extra：

```bash
uv tool install --reinstall 'noeta[tui,acp]'
```

在 Zed 里，把它加进 `settings.json`：

```json
{
  "agent_servers": {
    "noeta": { "command": "noeta", "args": ["acp"] }
  }
}
```

模型和 provider 和终端界面一样，读 noeta 自己的设置（编辑器的环境变量会传给这个进程，所以在那里设 `NOETA_MODEL` 和 `NOETA_API_KEY` 也行）。`args` 里写上 `--effort`（或者用 `NOETA_EFFORT`、`config.toml` 的 `effort`）和 `--permission-mode`（如 `["--permission-mode", "acceptEdits", "acp"]`），就定下了每个新会话起步时的推理强度和权限模式。

对应关系（`noeta/host/acp.py`）：

- **一个 ACP 会话就是一个 noeta 任务。** `session/new` 在编辑器的目录里建任务；`session/load` 打开已有任务并重放历史。任务记下的东西一落进日志，就翻译成 ACP 的会话更新：文字、思考过程、带类别和 diff 的工具调用、计划（任务清单）。
- **审批**以 ACP 权限请求的形式交给编辑器，选项和终端里的卡片一样（改文件的带 diff）；子代理的审批也一样冒上来。ACP 没有提问表单：`AskUserQuestion` 以文字加选项的形式显示，你的下一句话就是回答。
- **模式和模型**对应会话的 modes（任务提供的权限模式）和一个模型选项（目录里的模型和别名，外加 `default`，即代理自己的模型）。
- **斜杠命令**是代理插件提供的命令，再加适配层自己的四个：`/model [name]`、`/context`、`/cost`、`/rewind [n] [both|conversation|code]`（文件在本地改回去；那句提问之前的对话在一个新会话里继续，需要你去加载）。
- **编辑器为会话指定的 MCP 服务器**只交给这棵任务树的 MCP 插件，并且只放在内存里：它们的环境变量和请求头是凭据，绝不进任务日志。
- 提问里的**图片和嵌入的上下文**都能接收。

## 连到服务器

终端界面和 `noeta acp` 都可以不在本地跑，改为操作服务器上的任务；网页版和桌面版本来就是这样。

```bash
# 在服务器上
export NOETA_SERVER_TOKEN=$(openssl rand -hex 32)
noeta serve --host 0.0.0.0 --allow-host api.internal

# 在你的机器上
export NOETA_SERVER_TOKEN=...                   # 同一个令牌
noeta --server http://api.internal:8642         # 终端界面
noeta acp --server http://api.internal:8642     # 编辑器（写进 agent_servers）
```

`noeta serve` 总是要求 bearer 令牌（只有监听本机回环地址时才能用 `--no-token`）；没设 `NOETA_SERVER_TOKEN` 时，它第一次启动会生成一个、只打印这一次，`~/.noeta/server-token` 里只存它的 SHA-256。客户端启动前会带着令牌请求一次 `GET /agents`，不通就说清楚哪里不对（`noeta/cli/server_token.py`）。

连到服务器的前端不需要自己的模型。新任务用服务器的模型、在服务器的目录里跑，除非 `--model` 指定了模型，或 `--cwd PATH` 指定了服务器上的目录（不在服务器目录和它的 `--allow-root` 范围内的会被拒绝）。插件、代理、技能和设置都是服务器那边的：`--plugin-dir` 以及其他影响本机任务的参数（`--settings`、`--mcp-config`、`--allowedTools` 等）会被拒绝，`/reload-plugins` 会让你去重启工人。请求只能设置客户端被允许设置的那些任务配置（模型、推理强度、权限模式等）；服务器启动时没加 `--allow-bypass-permissions`，`bypassPermissions` 一律拒绝。`noeta -p` 总在本地跑，拒绝 `--server`；程序想对服务器用无界面协议，就连它的 `/stream-json` WebSocket。

部署、工人、工作区和令牌的细节见[作为服务端运行](./server.md)；所有接口见 [HTTP / WebSocket 接口](../reference/http-api.md)。
