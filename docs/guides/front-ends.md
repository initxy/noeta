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

The web app is a React page over `noeta serve`'s HTTP API (the same calls `RemoteClient`
makes). It has a sidebar of conversations grouped by directory, a streamed markdown transcript, tool
cards (Bash output, edits as diffs, sub-agents nested), approval cards with Claude Code's
choices, `AskUserQuestion` forms, plan review, the todo panel, the slash-command menu, `@`
mentions completed from the task's directory, image paste and drop, mode and model pickers,
usage and context fill, rewind, rename, archive and search, light and dark themes and a
phone-sized layout (`web/screenshots/README.md` lists each with a screenshot).

Build it, then let the server serve it:

```bash
cd web && npm install && npm run build      # writes web/dist
noeta serve --web web/dist                  # needs the server extra
```

`noeta serve` prints its bearer token the first time it makes one; open the address it prints
(`http://127.0.0.1:8642/` by default) and the page asks for the token once. You can also hand it
over in the address: `http://127.0.0.1:8642/#token=…` (the part after `#` never reaches the
server; the page keeps the token in the browser's storage and drops it from the address).
"Disconnect" forgets it.

The page's files are served without a token (they hold none) with a content security policy
that lets the page talk only to its own server. A page served from somewhere else may call the
API only from an origin you name: `noeta serve --allow-origin https://ui.example.com`.

For working on the page itself, `npm run dev` in `web/` runs Vite and proxies the API to a
running `noeta serve` (`NOETA_SERVER`, default `http://127.0.0.1:8642`).

What it does not have: cloud environments, repository and branch pickers (noeta runs in the
server's directories), a terminal pane and a file tree. A conversation cannot be deleted, since the
store keeps every task's log; archive hides it.

## The desktop app

The desktop app is the web app in an Electron window over a local `noeta serve` it starts and
stops. Nothing to connect or paste: on launch it finds `noeta`, picks a free loopback port,
makes a fresh token for that run, and starts

```text
noeta serve --host 127.0.0.1 --port <free port> --web <the built web app> [--allow-root DIR …]
```

with the token in the server's environment (`NOETA_SERVER_TOKEN`), never on its command line
(`desktop/src/server.ts`). Quitting stops the server.

```bash
cd desktop && npm install
npm run web          # build ../web first
npm start            # build and open the app
npm run dist         # installers: macOS dmg, Linux AppImage and deb (not signed)
```

It looks for `noeta` on `PATH` and in `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`
(an app started from the Dock does not get the login shell's `PATH`); set
`NOETA_DESKTOP_NOETA` or `noetaPath` in the app's `settings.json` (Settings, `⌘,`) to name
another. When it is missing the window shows the install line, which includes the `server`
extra. File > Open Project Folder (`⌘O`) picks the directory new conversations run in. The server runs
in your home directory, so a folder under it needs nothing more; a folder outside it is added as an
`--allow-root` of the server, which restarts it (after asking, when sessions are working or
waiting; `desktop/src/main.ts`). When the local server dies the window shows its
last output, and View > Reload starts it again. It also raises a system notification when a
conversation needs you or finishes while the window is in the background.

The desktop app always runs its own local server; to work against a remote one, open the web
app that server serves.

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
