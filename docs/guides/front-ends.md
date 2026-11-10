# Web, desktop and editors

Besides the [terminal UI](./terminal.md), noeta has a web app (`web/`), a desktop app
(`desktop/`) and an Agent Client Protocol agent for editors (`noeta acp`). This page says how to
run each and how to attach any of them to a server.

## One API underneath

Every front end is a translation of the same client API (`noeta/core/client.py`: create, send,
answer, cancel, read, watch). None of them holds task state: what they show is read from the
task's log, so the same task can be opened in the terminal, then in the browser, then in an
editor, and each sees the whole conversation.

<Diagram name="guide-ways-in" caption="Every way in ends at the one client API; the store and the workers behind it are the same" />

| Front end | Talks to | Code |
|---|---|---|
| Terminal UI | `AppAPI`: `LocalApp` in process, `RemoteApp` with `--server` | `tui/`, `noeta/host/app.py`, `noeta/host/remote.py` |
| `noeta -p`, Agent SDK | Claude Code's stream-json protocol, in process | `noeta/cli/headless.py`, `noeta/host/stream_json.py` |
| Web app | `noeta serve`'s HTTP API | `web/src/api/client.ts`, `noeta/host/http.py` |
| Desktop app | the web app, over a `noeta serve` it starts | `desktop/src/server.ts` |
| Editors (Zed, JetBrains, Neovim, …) | ACP on stdio, in process or `--server` | `noeta/host/acp.py` |
| Your program | `noeta.Client` / `noeta.LocalApp`, or `noeta.RemoteClient` | [Python API](../reference/python-api.md) |

A new outside protocol is another translation in `noeta/host`, never a second API.

## The web app

The web app ships inside the `noeta` package: there is nothing to build. With the `server`
extra installed (`uv tool install 'noeta[tui,server]'`), open it with

```bash
noeta web            # starts a local server in the background and opens the page, signed in
noeta web status     # is it running, and where
noeta web stop       # stops it
```

`noeta web` starts a `noeta serve` on `127.0.0.1` in the background with the web app noeta's
wheel carries, and opens it in your browser, signed in; a second `noeta web` reuses the running
server ([the noeta command](../reference/cli.md#noeta-web)).

It is laid out as a local workbench. The left column starts with **New session**, then lists
your projects: folders, by name (hover for the path), each with its sessions newest first, a
status dot (running, needs you, waiting, ended) and how long ago each last moved. A project is a
folder you added, or the folder of a session you already have. Pick a project and the main pane
shows its name and path above the composer: the next session runs there. A project's **+** starts
a session in it directly; its **⋯** menu removes a project you added (its sessions stay). The
search box narrows every project's sessions at once; **Archived** at the bottom lists the
archived ones. The page remembers the project you last opened.

**Add project** (the **+** beside "Projects") opens a folder picker over the server's folders:
walk into one or type a path, then add it. The server only runs sessions in its own directory and
the folders it was allowed (`--allow-root`; `noeta web` allows your home directory), and the
picker says so when you try a folder outside them.

The main pane has a streamed markdown transcript, tool cards (Bash output, edits as diffs,
sub-agents nested), approval cards with Claude Code's choices, `AskUserQuestion` forms, plan
review, the todo panel, the slash-command menu, `@` mentions completed from the session's folder,
image paste and drop, mode and model pickers, usage and context fill, rewind, rename, light and
dark themes and a phone-sized layout (`web/screenshots/README.md` lists each with a screenshot).

### Against another server

Any `noeta serve` serves the same page. It prints its bearer token the first time it makes one;
open the address it prints (`http://127.0.0.1:8642/` by default) and the page asks for the token
once. You can also hand it over in the address: `http://127.0.0.1:8642/#token=…` (the part after
`#` never reaches the server; the page keeps the token in the browser's storage and drops it from
the address). "Disconnect" forgets it.

The page's files are served without a token (they hold none) with a content security policy
that lets the page talk only to its own server. A page served from somewhere else may call the
API only from an origin you name: `noeta serve --allow-origin https://ui.example.com`.

What it does not have: cloud environments, repository and branch pickers (noeta runs in the
server's directories), a terminal pane and a file tree. A session cannot be deleted, since the
store keeps every task's log; archive hides it.

### Developing the page

The page's source is `web/` (React and Vite). `npm run dev` there runs Vite and proxies the API
to a running `noeta serve` (`NOETA_SERVER`, default `http://127.0.0.1:8642`); `npm run build`
writes `web/dist`, which `noeta serve --web web/dist` serves instead of the bundled copy.
`make frontends` runs every front end's tests.

## The desktop app

The desktop app is the web app in its own window, over a local `noeta serve` it starts and
stops. Installers for macOS (`.dmg`) and Linux (`.AppImage`, `.deb`) are attached to each
[GitHub Release](https://github.com/initxy/noeta/releases). They are not signed: on macOS,
right-click the app and choose **Open** the first time (double-clicking says it cannot be
checked); an AppImage needs `chmod +x` first. The app does not carry noeta itself: install it
too (`uv tool install 'noeta[tui,server]'`); when it is missing the window shows that line.

Nothing to connect or paste: on launch it finds `noeta`, picks a free loopback port, makes a
fresh token for that run, and starts

```text
noeta serve --host 127.0.0.1 --port <free port> --web <the app's web files> [--allow-root DIR …]
```

with the token in the server's environment (`NOETA_SERVER_TOKEN`), never on its command line
(`desktop/src/server.ts`). Quitting stops the server.

It looks for `noeta` on `PATH` and in `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`
(an app started from the Dock does not get the login shell's `PATH`); set
`NOETA_DESKTOP_NOETA` or `noetaPath` in the app's `settings.json` (Settings, `⌘,`) to name
another.

**Add project** (the column's **+**, File > Add Project…, `⌘O`) opens the system's folder
picker and saves the folder as a project. The server runs in your home directory, so a folder
under it needs nothing more; a folder outside it is added as an `--allow-root` of the server,
which restarts it (after asking, when sessions are working or waiting; `desktop/src/main.ts`).
The window opens on the project you last used. When the local server dies the window shows its
last output, and View > Reload starts it again. It also raises a system notification when a
session needs you or finishes while the window is in the background.

The desktop app always runs its own local server; to work against a remote one, open the web
app that server serves.

To build it yourself: `cd desktop && npm install && npm start` (it uses `../web/dist`; `npm run
web` builds it), and `npm run dist` writes the installers to `desktop/release/`.

## Plugins in the web and desktop apps

The plugins button in the header (or `/plugins`) opens the plugins dialog. **Installed** lists
the agent's plugins with their parts and anything wrong with them: switch one off for this
session (from its next turn on; the `config` signal's `plugins_off`) or for new sessions (the
machine's default), or remove one ([which plugins can be switched](../reference/plugins.md#switching-a-plugin-off)). **Discover** lists what the configured marketplaces offer
and installs one only after showing its source, commit and parts; a path or a pinned git URL
can be typed in too. Installing and removing over HTTP work on a loopback server (the desktop
app's) or one started with `--allow-plugin-install`; anywhere else the dialog says so.

What a session's plugins show without code of their own (`Extension.ui`) appears under the
composer: status items in the status line, widgets above or below the box, notices that close.
The page asks for them again shortly after the session's log moves.

A plugin with a web part (see [Write a plugin › A web part](./plugins.md#a-web-part)) runs in
the page while the open session runs it: the page imports its module from the server, calls
its `activate(ctx)`, and withdraws everything it added when it is switched off or you leave the
session. Its components go into the page's slots: `statusbar` (the line under the composer,
after the model picker; a negative `order` puts one before it), `sidebar` (the column beside
the conversation), `settings` (the dialog's Settings tab), `session.header` (the session's
title), `composer`, `tool:<Tool>` (a tool card's body), `request:<Tool>` (what an approval or
question card shows of the call it asks about), `record:<plugin>/<kind>` (a record in the
conversation), `codeblock:<language>` (a fenced block in rendered markdown) and `theme`. A
contribution that throws shows its plugin's name in its place, and the dialog names a plugin
whose web part failed to load or that waited in vain for another plugin's slot. In the desktop
app `ctx.desktop` is its bridge (`project`, `chooseProject`, `setProject`, `notify`); in a browser it is
`null`.

The built-in plugins' views come the same way, from their own web parts in the page's
`web/src/plugins/<name>/`: the task list (`todos`), the permission mode picker and Shift+Tab
(`permissions`), the plan to review (`plan_mode`), the auto-compact countdown (`compaction`),
the session's title (`title`), the shell and file tools' cards with their diffs (`shell`,
`fs`), sub-agents and workflow runs (`subtasks`, `workflow`), review findings (`findings`), and
the Settings tab's memory, MCP and hooks sections. Switch one off for a session and its view
goes with it; a tool no plugin gives a view of gets a plain card (its input, its output).
Approvals and questions, rewind, the conversation and the model picker are the page's own.

## Editors: `noeta acp`

`noeta acp` serves the [Agent Client Protocol](https://agentclientprotocol.com) on stdin and
stdout, so Zed, JetBrains IDEs, Neovim and other ACP clients can run noeta as their agent. It
needs the `acp` extra:

```bash
uv tool install --reinstall 'noeta[tui,acp]'
```

In Zed, add it to `settings.json`:

```json
{
  "agent_servers": {
    "noeta": { "command": "noeta", "args": ["acp"] }
  }
}
```

Model and provider come from noeta's own settings, as for the terminal UI (the editor's
environment is passed to the process, so `NOETA_MODEL` and `NOETA_API_KEY` set there work too).
`--effort` (or `NOETA_EFFORT`, `config.toml` `effort`) and `--permission-mode` in `args`
(`["--permission-mode", "acceptEdits", "acp"]`) set every new session's starting effort and mode.

How it maps (`noeta/host/acp.py`):

- **One ACP session is one noeta task.** `session/new` creates a task in the editor's directory;
  `session/load` opens an existing one and replays its history. What the task records is
  translated into ACP session updates as it lands: text, thoughts, tool calls with their kind and
  diff, the plan (todos).
- **Approvals** go to the editor as ACP permission requests, with the same choices as a card in
  the terminal (an edit's diff on it); a sub-agent's approvals come up the same way. ACP has no
  question form: an `AskUserQuestion` shows as text with its options, and your next prompt
  answers it.
- **Modes and model** are the session's modes (the permission modes the task offers) and a
  model option (the catalog's models and aliases, plus `default`, the agent's own model).
- **Slash commands** are the agent's plugin commands, plus four of the adapter's own:
  `/model [name]`, `/context`, `/cost` and `/rewind [n] [both|conversation|code]` (files are put
  back here; the conversation before that prompt goes on in a new session to load).
- **MCP servers the editor names** for a session are passed to the MCP plugin for that task tree
  only, and kept in memory: their environment and headers are credentials and never enter the
  task's log.
- **Images and embedded context** in a prompt are accepted.

## Attach to a server

The terminal UI and `noeta acp` can drive a server's tasks instead of running them here; the web
and desktop apps always do.

```bash
# on the server
export NOETA_SERVER_TOKEN=$(openssl rand -hex 32)
noeta serve --host 0.0.0.0 --allow-host api.internal

# on your machine
export NOETA_SERVER_TOKEN=...                   # the same token
noeta --server http://api.internal:8642         # the terminal UI
noeta acp --server http://api.internal:8642     # an editor (put this in agent_servers)
```

`noeta serve` always requires a bearer token (`--no-token` only on a loopback address); without
`NOETA_SERVER_TOKEN` it makes one on first start, prints it that once, and keeps only its SHA-256
in `~/.noeta/server-token`. Before it starts, a client checks the server answers `GET /agents`
with the token and says what is wrong when it does not (`noeta/cli/server_token.py`).

A front end attached to a server needs no model of its own. A new task runs on the server's
model and in the server's directory, unless `--model` names one or `--cwd PATH` names a
directory on the server (the server refuses one outside its directory and its `--allow-root`
paths). Plugins, agents, skills and settings are the server's: `--plugin-dir` and the other flags
that shape the tasks run here (`--settings`, `--mcp-config`, `--allowedTools`, …) are refused, and
`/reload-plugins` asks you to restart the workers. A request may set only the task config a
client may (model, effort, permission mode, …); `bypassPermissions` is refused unless the server
was started with `--allow-bypass-permissions`. `noeta -p` always runs here and refuses
`--server`; a program that wants the headless protocol against a server uses its `/stream-json`
WebSocket.

Deployment, workers, workspaces and the token in depth: [Run it as a server](./server.md); every
endpoint: [HTTP / WebSocket API](../reference/http-api.md).
