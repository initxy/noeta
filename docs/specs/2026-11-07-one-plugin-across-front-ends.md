---
status: active
created: 2026-11-07
branch: spec/unified-plugins
---

# One plugin across the kernel and every front end

## Goal

A plugin is one name and one switch, and may carry up to four parts: the backend `Extension`
it has today, UI data every front end shows, web code (web app and desktop app), and later
TUI code. Plugins are switched on and off from the terminal UI, the web app and the desktop
app, per machine and per task. The UI that front ends hard-code today for built-in plugins
moves under those plugins' names and goes through the same mechanism a third party uses.
Optional official plugins and the official marketplace live in a separate repository.

## Non-goals

- A sandbox or a permission manifest for plugin code. A plugin runs with noeta's rights, as
  in pi, DeepSeek Harness and Claude Code; trust stays "installed, enabled, project trusted".
- A hosted marketplace service (review, ratings, accounts). A marketplace is a static file.
- Rendering HTML from plugins or MCP servers in iframes (MCP Apps); a later spec if asked for.
- The TUI code part (`activate(ctx)` in Python). The UI data part covers the TUI in this
  spec; the code part gets its own spec once the web code part has shipped.
- New front-end features (file tree, terminal pane, task board, Git panel, themes). They are
  plugins for later, in `noeta-plugins` or third-party.
- Renaming built-in plugins or changing how existing file, package and Claude Code plugins load.

## Context

What exists (all on `main` @0f172a10, 1.0.6):

- One backend interface, `Extension` (`noeta/core/extension.py`), found through entry points
  or plugin files (`noeta/core/registry.py`, ADR-002). A task records the plugins it runs
  with (`records.Plugins`, `registry.pin`); the set comes from its agent
  (`noeta/core/runner/task.py:412`, `Agent.all_extensions`).
- Switching a plugin off is `[plugins.<name>] enabled = false` in `config.toml`, applied by
  `/reload-plugins` in a local TUI only (`noeta/cli/config.py:756`); a server needs a rolling
  restart. Web and desktop have no switch; the HTTP API has `GET /commands` but no plugin
  listing (`noeta/host/http.py:244`).
- Front ends reach plugins only through slash commands and a few records read by name
  (`noeta/toolkit/plugin_records.py`); the todo panel, sub-agent and workflow progress, mode
  picker, context gauge and tool cards are hard-coded (`web/src/components/`,
  `tui/src/noeta_tui/`). `lint-imports` forbids front ends importing `noeta.plugins`.
- The desktop app is the web app over a local `noeta serve`, plus a small bridge
  (`desktop/src/preload.ts`, `web/src/desktop.ts`). The web build is not in the Python wheel.
- `noeta plugin add` installs pinned git commits, paths and Claude Code marketplace entries
  into `~/.noeta/plugins/`, running nothing from the plugin (`noeta/cli/plugin_command.py`).

What others do (read 2026-11-07; clones and full notes were in the session scratchpad):

- pi (`badlogic/pi-mono` @085c84e9, coding-agent 1.5.0): TypeScript extensions loaded with
  jiti; built-ins (MCP, tool search) are extensions marked `replaceable`. UI is terminal-first
  at fixed places; in RPC mode only `select/confirm/input/editor`, `notify`, `setStatus(key)`,
  `setWidget(key, lines, placement)` cross to the client (pi's RPC extension-UI doc). The
  experimental `chord`/`apps/p2` splits one plugin into `server.ts`/`web.tsx`/`tui.ts` with a
  versioned contract and named slots. Packages: npm/git/local, pinned, no install scripts;
  pi.dev lists npm packages with keyword `pi-package` (~5300), no review.
- DeepSeek Harness (`deepseek-ai/DeepSeek-Harness` @fcc0ad46, 0.2.2-rc.1, plus
  DSH-better-sidebar, dsh-market, dsh-genui, dsh-TUI, dsh-at-file): one Cordis plugin table
  for server and browser; a plugin's `./client` bundle is served at runtime and shares a fixed
  list of modules (react, react-dom, ui-slots, …); typed slots with `single/list/keyed/
  chain/hoc` mounts and auto-withdrawal. Pain points: dependencies that never appear hang a
  plugin silently (postmortem 0001), a dozen host peer dependencies per plugin, separate web
  and desktop profiles, pnpm build approvals exposed to users, no code-block slot (dsh-genui
  patches the DOM), the TUI ecosystem invented its own contribution protocol. Marketplace =
  static index JSON, no default source shipped.
- Claude Code: `/plugin` enables and disables installed plugins (`enabledPlugins`), a
  marketplace is a `marketplace.json`, the only UI extension is the `statusLine` command.

## Decisions

Owner, 2026-11-07: follow the proposals in the design discussion: optional official plugins
in a separate repository, the official marketplace on by default, the TUI code part deferred,
`repetition_guard` moved out. Details below are this spec's, open to review.

- D1 Plugin shape. A plugin may ship `.noeta-plugin/plugin.json` beside its code (next to
  the module for a package): `name`, `version`, `description`, `ui_api` (an integer), `web`
  (path to a built ES module). No manifest = backend only, exactly today's behaviour. The
  manifest names no host dependencies: `ui_api` is the one compatibility number and the host
  supports a range of it.
- D2 One switch, three scopes. `[plugins.<name>] enabled = false` in `config.toml` still
  removes a plugin from the agents (the hard switch). A task's own switch is a new
  `CHANGEABLE_CONFIG` key, `plugins_off: list[str]`, sent with the existing `config` signal
  (`noeta/core/loop/state.py`): a plugin named there keeps folding its records (state,
  snapshots and the fold version do not change) but offers nothing and is asked nothing
  (tools, context parts, reminders, gates, commands, `ui`, `on_event`) from the next turn on,
  on whichever worker runs the task. Sub-agents inherit the parent's `plugins_off`. The
  machine default for new tasks is `~/.noeta/plugin-switches.json` (`{"off": [...]}`),
  written by `PUT /plugins/{name}`, and joins every new task's `plugins_off`. Front ends offer
  a switch only for installed (non-built-in) plugins and the built-ins safe to drop: `web`,
  `workflow`, `schedule`, `todos`, `findings`, `title`, `quiet_nudge`, `memory`, `mcp`,
  `checkpoints`, `skills`, `ask_user`.
- D3 UI data part (all front ends). New optional `Extension.ui(view) -> Sequence[UiItem]`,
  computed from the folded view like reminders: `StatusItem(key, text)`, `Widget(key,
  markdown, placement="above"|"below")`, `Notice(text, level)`. Dialogs stay the existing
  approvals and questions. Served by `GET /tasks/{id}/ui` and `AppAPI.ui(task_id)`; front ends
  fetch it again when the watched log advances.
- D4 UI to backend calls. New optional `Extension.ui_call(ctx, name, args) -> JSON`, reached
  by `POST /plugins/{name}/call` (with `task_id`) and `AppAPI.plugin_call`. Anything lasting
  it changes goes into the task log (a signal or a record), never plugin-private state.
- D5 Web code part. `web` points at a prebuilt ES module exporting `activate(ctx)`.
  `noeta serve` serves an enabled plugin's web directory at `/plugins/{name}/web/` (same
  origin, the current CSP holds). `GET /plugins` lists name, source, enabled, broken reason,
  parts, `ui_api`, web entry URL with a content hash. The page provides shared modules through
  a native import map: `react`, `react-dom`, `react/jsx-runtime`, `@noeta/web-sdk`,
  `@noeta/ui`; a plugin marks them external and bundles anything else. A `ui_api` outside the
  host's range leaves the web part unloaded, with the reason on the plugins page.
- D6 Slots and `ctx` v1. Slots: `statusbar`, `sidebar`, `settings`, `session.header`,
  `composer`, `tool:<Tool>`, `record:<plugin>/<kind>`, `codeblock:<language>`, `theme`.
  Mounts: `single`, `list`, `keyed`, `wrap`. A contribution is withdrawn when its plugin is
  switched off. Waiting for another plugin's slot times out after 5 s and the plugins page
  names the waiting plugin. Every contribution renders inside its own error boundary.
  `ctx` v1: `client` (read, watch, send, answer, signal for the current task), `records(kind)`
  (the plugin's own), `call(name, args)` (D4), `slots`, `theme`, `desktop` (D7) — nothing else.
- D7 Desktop is the web app; `ctx.desktop` is the existing bridge (`notify`, `project`,
  `chooseProject`) and `null` in a browser. No separate desktop plugin install or profile.
- D8 Built-in UI moves under plugin names, in the front ends' trees, registered through the
  same slot API: `web/src/plugins/<name>/` (later `tui/src/noeta_tui/plugins/<name>.py`).
  Shown only while the task runs that plugin. `todos` → `sidebar`; `subtasks`, `workflow` →
  `tool:Agent`, `tool:Workflow`; `fs`, `shell` → `tool:Edit`/`Write`/`Read`/`Bash`;
  `permissions`, `plan_mode` → `statusbar`, `record:plan_mode/*`; `compaction` →
  `statusbar`; `findings` → `record:findings/*`; `mcp`, `hooks`, `memory` → `settings`;
  `title` → `session.header`. Approval and question dialogs, rewind, the transcript and the
  model and effort pickers stay host UI.
- D9 Where plugins live. One install per machine in `~/.noeta/plugins/`, read by the TUI, a
  local `noeta serve` and the desktop app alike; a project's `.noeta/plugins/` after trust; a
  server's own `~/.noeta/plugins/`. Only built artefacts are installed: a plugin with a web
  part commits its built `web` directory or ships it as package data. Installing runs no npm,
  no build and no plugin code.
- D10 Marketplace. Claude Code's `marketplace.json` plus a `noeta` object per entry
  (`ui_api`, parts). `noeta plugin marketplace add|list|remove`; the official marketplace
  (`initxy/noeta-plugins`) is configured by default and read from its default branch over
  https, while each entry still pins a commit (today's rule for entries). The plugins page has
  Installed (switch, configure, remove) and Discover (install after a confirmation showing
  source, commit and parts). Install and remove over HTTP are allowed only when `noeta serve`
  listens on loopback (the desktop app) or runs with `--allow-plugin-install`.
- D11 Repositories. `initxy/noeta-plugins` holds optional official plugins, one directory
  each, its `marketplace.json`, and CI against the latest noeta. `repetition_guard` (not
  Claude Code's, off by default) and `examples/chat-bridge` move there; a `config.toml` that
  still names `repetition_guard` gets the existing missing-plugin notice with its install
  line. `hello-plugin`, `run-tests` and `house-style` stay in `examples/`, joined by a web-part
  example. `web/sdk` (`@noeta/web-sdk`: `ctx` types, slot names, a build template) and `web/ui`
  (`@noeta/ui`) are published to npm at noeta's version.
- Owner, 2026-11-07 (follow the recommendations): `examples/chat-approvals` moves to
  `noeta-plugins` with `chat-bridge`, the only plugin it works with. The npm scope is
  `@noeta-ai` (`@noeta` is taken). Creating the GitHub repository and publishing to npm wait
  for the owner; until then `noeta-plugins` is a local repository beside this one.

## Interfaces (v1)

Python (`noeta/core/extension.py`, exported from `noeta`):

```python
@dataclass(frozen=True)
class StatusItem: key: str; text: str; tone: str = "normal"      # normal | ok | warn | error
@dataclass(frozen=True)
class Widget: key: str; markdown: str; placement: str = "above"  # above | below the composer
@dataclass(frozen=True)
class Notice: key: str; text: str; level: str = "info"           # info | warn | error
UiItem = StatusItem | Widget | Notice

class Extension:
    async def ui(self, view: TaskView) -> Sequence[UiItem]: return ()
    async def ui_call(self, ctx: CommandContext, name: str, args: Any) -> Any:
        raise LookupError(name)
```

`AppAPI` (`noeta/host/app.py`; `LocalApp`, `RemoteApp`):

- `plugins(agent, cwd) -> list[PluginInfo]`; `set_plugin_default(name, enabled)`;
  `ui(task_id) -> list[UiEntry]`; `plugin_call(task_id, plugin, name, args) -> Any`;
  `install_plugin(source, marketplace=None, commit=None) -> PluginInfo`;
  `remove_plugin(name)`; `market() -> list[MarketEntry]`. A task's switch is
  `change_config(client, task_id, {"plugins_off": [...]})`.
- `PluginInfo`: `{name, version, description, source, builtin, enabled, switchable, broken,
  parts, ui_api, web}`; `parts` ⊂ `backend, ui, web, skills, agents, commands, hooks, mcp`;
  `web` is `{"entry": "/plugins/<name>/web/<file>?v=<hash>"}` or `null`.
- `UiEntry`: `{plugin, type: "status"|"widget"|"notice", key, text|markdown,
  tone|placement|level}`.
- `MarketEntry`: `{marketplace, name, description, version, source, commit, parts, ui_api,
  installed}`.

HTTP (`noeta/host/http.py`; token as today unless noted):

| Route | Body / answer |
|---|---|
| `GET /plugins?agent=&cwd=` | `{"plugins": [PluginInfo], "ui_api": [min, max]}` |
| `PUT /plugins/{name}` | `{"enabled": bool}` → machine default for new tasks |
| `POST /plugins/{name}/call` | `{"task_id", "name", "args"}` → `{"result": ...}` |
| `GET /plugins/{name}/web/{path}` | the plugin's built web files, **no token** (an ES module import sends no header; the files hold no secret), only for an enabled plugin |
| `GET /tasks/{id}/ui` | `{"items": [UiEntry]}` |
| `GET /market` | `{"entries": [MarketEntry]}` |
| `POST /plugins/install`, `DELETE /plugins/{name}` | loopback or `--allow-plugin-install` only (403 otherwise) |

Web (`@noeta-ai/web-sdk`): a plugin's module exports
`activate(ctx: PluginContext): void | (() => void)`. `ctx.slots.add(slot, {key?, order?,
mount?}, Component)` returns a remover; `ctx.slots.wait(slot, timeoutMs = 5000)`. The page's
import map serves `react`, `react-dom`, `react-dom/client`, `react/jsx-runtime`,
`@noeta-ai/web-sdk`, `@noeta-ai/ui` as the page's own single instances.

## Plan

### 1. Listing and switches
- [x] `GET /plugins`, `AppAPI.plugins()` (`LocalApp`, `RemoteApp`): name, source, built-in,
      enabled, switchable, broken reason, parts.
- [x] The `plugins` task signal, folded off-set, the runner filter, sub-agent inheritance,
      pins recorded again; tests across two workers.
- [ ] TUI `/plugins` menu; web settings page "Plugins" › Installed; desktop gets it for free.
- [ ] Docs (en + zh): front ends guide, plugins guide, HTTP API reference.

### 2. UI data part
- [x] `UiItem` types, `Extension.ui`, `GET /tasks/{id}/ui`, `AppAPI.ui`.
- [ ] Renderers: TUI status line and widgets; web status bar, widgets, notices.
- [ ] `examples/hello-plugin` shows a status item; docs.

### 3. Web code part
- [ ] Manifest reading (`.noeta-plugin/plugin.json`) for files, directories and packages.
- [x] `/plugins/{name}/web/` serving, `POST /plugins/{name}/call`, `Extension.ui_call`.
- [ ] Web: import map, loader, slot registry with mounts, timeouts, error boundaries, `ctx` v1.
- [ ] `web/sdk`, `web/ui` packages and a build template; `examples/web-panel`; e2e test.

### 4. Built-in UI under plugin names
- [ ] `todos` and `compaction` first (proves `sidebar` and `statusbar`).
- [ ] `subtasks`, `workflow`, `permissions`, `plan_mode`, `findings`, `title`, `mcp`,
      `hooks`, `memory`.
- [ ] `fs` and `shell` tool cards last; the web tree keeps no plugin-specific component
      outside `web/src/plugins/`.

### 5. Marketplace
- [ ] `noeta plugin marketplace add|list|remove`, the default official marketplace, `noeta`
      entry fields.
- [ ] Plugins page › Discover; install and remove endpoints with the loopback /
      `--allow-plugin-install` guard.

### 6. `noeta-plugins` repository
- [ ] Create it with `marketplace.json` and CI; move `repetition_guard` and `chat-bridge`;
      settle `chat-approvals`; CHANGELOG note for the move.

### 7. Wrap-up
- [ ] ADR refining ADR-002 (front-end parts of one plugin), `CONTEXT.md` entry, archive.

## Acceptance criteria

- Switching a plugin off for one task in the web app drops its tools and UI from that task's
  next turn on, on whichever worker runs it, and leaves other tasks untouched; the TUI and
  desktop show the same.
- A third-party plugin directory with a backend part, a status item and a web sidebar panel
  installs with `noeta plugin add` and works in TUI (status item), web and desktop without any
  change to noeta's repository.
- No component under `web/src/components/` reads a specific plugin's records; each built-in
  plugin's UI lives under `web/src/plugins/<name>/` and disappears when the plugin is off.
- A web part that throws, or waits on a slot that never comes, leaves the page working and is
  named on the plugins page.
- Installing from Discover shows source, commit and parts before anything is fetched, and is
  refused over HTTP on a non-loopback server without `--allow-plugin-install`.
- `make check`, web unit and e2e tests and the docs check are green.

## Risks

- `ctx` and the slot names become a public contract once published; v1 stays small and
  `ui_api` versions it.
- A web part runs with the page's rights (token, every endpoint); the install confirmation and
  the HTTP install guard are the only checks, as in the projects compared.
- Fetching `GET /tasks/{id}/ui` on every log advance could cost on busy tasks; debounce, and
  measure before adding a push channel.
- Moving `repetition_guard` out breaks setups that enabled it until they install it.
- Publishing npm packages is new release work (`docs/releasing.md`).

## Progress log

- 2026-11-07 Written after reading pi, DeepSeek Harness and five community plugins; proposed.
- 2026-11-07 Owner: start, following every recommendation; active. D2 moved onto the `config` signal (`plugins_off`); interfaces fixed for parallel work.
- 2026-11-07 Kernel and host half: `plugins_off` (folded as `TaskState.plugins_off`, taken from the config only while no turn is open; `TaskRun.exts` is the filtered list, `all_exts` folds and pins), `UiItem`s, `ui`/`ui_call`, the `AppAPI` plugin methods with `LocalApp`/`RemoteApp`, the plugin routes (`noeta/host/_http_plugins.py`, listing in `noeta/host/plugins.py`), machine switches and install/remove/market wiring (`noeta/cli/plugin_switches.py`), `noeta serve --allow-plugin-install`, hello-plugin's status item, HTTP and Extension reference (en + zh). Open: a manifest beside a single-file plugin (only directories and packages read one); Claude Code plugins are listed but not switchable per task; a request's `plugins_off` may not name a built-in outside D2's list (403); with `--no-worker` `/tasks/{id}/ui` is empty and switches are 501; guides still to write.
