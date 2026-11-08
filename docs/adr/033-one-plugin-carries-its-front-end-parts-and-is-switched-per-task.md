# ADR-033: One plugin carries its front-end parts and is switched per task

- Status: accepted
- Date: 2026-11-07
- Refines: [ADR-002](002-one-extension-interface-and-entry-points.md)

## Context

ADR-002 made every capability a plugin behind `Extension`, but front ends stayed outside it:
the terminal UI and the web app hard-coded the views of built-in plugins (the todo panel,
sub-agent and workflow progress, the mode picker, tool cards), a third-party plugin could
reach a front end only through slash commands and questions, and the only switch was
`[plugins.<name>] enabled = false`, applied by a local `/reload-plugins` or a rolling restart.
The web and desktop apps had no switch at all. Users expect, as in Claude Code, pi and
DeepSeek Harness, to turn plugins on and off from the UI and to install ones that bring UI.

## Decision

- **A plugin is one name with up to these parts:** its `Extension` (the backend), UI data
  every front end shows, a web part, and later a TUI code part. Claude Code parts (skills,
  agents, commands, hooks, MCP) stay as they are. A manifest beside the plugin's code,
  `.noeta-plugin/plugin.json` (`name`, `version`, `description`, `ui_api`, `web`), names the
  front-end parts; no manifest means backend only. `ui_api` is the one compatibility number;
  the host runs a range of it (`noeta/host/plugins.py` `UI_API`).
- **UI data** is `Extension.ui(view)` → `StatusItem` / `Widget` / `Notice`, computed from the
  folded view like reminders and served by `GET /tasks/{id}/ui`; a plugin's own front-end code
  calls `Extension.ui_call`, and anything lasting it changes goes into the task's log.
- **The web part** is a prebuilt ES module exporting `activate(ctx)`, served by `noeta serve`
  from the plugin's own directory on the page's origin and loaded at runtime. The page provides
  `react`, `react-dom`, `react/jsx-runtime`, `@initxy/noeta-web-sdk` and `@initxy/noeta-ui` as its own
  single instances through a native import map (`web/index.html`, `web/shared-modules.ts`); a
  plugin marks them external and bundles anything else. `ctx` and the slot names
  (`web/sdk`) are the contract, versioned by `ui_api`. The desktop app is the web app;
  `ctx.desktop` is its bridge.
- **Built-in UI goes through the same slots.** Each built-in plugin's web view is its web
  part in `web/src/plugins/<name>/`, registered by name with the same `ctx` a third party
  gets and importing only what a third party could; the page's own code reads no plugin's
  records and names no plugin's tool (`web/src/plugins/boundary.test.ts`). Approvals,
  questions, rewind, the transcript and the model picker stay the page's.
- **Switched per task through the log.** `plugins_off`, a key of the task config changed by
  the existing `config` signal, names plugins the task runs without: from its next turn on,
  on whichever worker runs it, they keep folding their records (state, snapshots and fold
  version unchanged) but are asked nothing else; children inherit it. The machine's default
  for new tasks (`~/.noeta/plugin-switches.json`) joins each new task's `plugins_off`;
  `enabled = false` stays the hard switch. A request may switch off only installed plugins and
  the built-ins a task runs well without (`SWITCHABLE_BUILTINS`).
- **Marketplaces are static files with pinned entries.** A marketplace is Claude Code's
  `marketplace.json` (plus an optional `noeta` object per entry), read from a path, a pinned
  commit or a repository's default branch; each entry installs only the commit it pins, and
  installing runs nothing from the plugin. Installing over HTTP is allowed only on a loopback
  server or with `--allow-plugin-install`.
- **Optional official plugins live in a separate repository**, `initxy/noeta-plugins`, which is
  also the marketplace configured by default. Plugins noeta does not need to be Claude Code
  compatible (`repetition_guard`, the chat channels) live there.

## Rationale

One name and one switch per plugin means a person turns a capability off once and its tools,
prompt text and UI go together in every front end. Folding the switch from the log makes it
hold on every worker and survive restarts for free (ADR-001), where a per-process switch would
differ between machines; keeping the records folded keeps a task's state and snapshots
identical whichever plugins it runs with, so switching back on loses nothing. UI data covers
every front end, the terminal included, without any plugin code running in it. Sharing React
and the SDK through the import map gives one instance of each (hooks and context work across
plugins and host) and keeps a plugin's manifest free of host dependencies. Moving the
built-ins' UI under their names is what proves the slot API is enough, as the built-in
backends prove `Extension` is (ADR-002). A static marketplace needs no service and no
accounts, and pinned commits keep a listing that moves from changing what is installed.

Rejected:

- **An ecosystem per front end** (DeepSeek Harness's TUI plugins invented their own
  contribution protocol beside its web plugins): two contracts, plugins that work in one front
  end only, and a switch per front end.
- **Plugin JavaScript outside an import map, each bundling its own React** or loaded through
  a global script: duplicate React instances break hooks and context, and plugins grow host
  peer dependencies (a dozen per plugin in DeepSeek Harness) that drift with the host.
- **A hosted marketplace** with review, ratings and accounts: a service to run and trust for
  what a static file and a pinned commit already give.
- **Iframes for plugin UI** (as MCP Apps do): isolation the trust model does not need (a
  plugin's backend already runs with noeta's rights), at the cost of shared theme, slots and
  React state; left for a later decision if HTML from MCP servers is wanted.

## Consequences

- `ctx`, the slot names and the `UiItem` types are public contracts once `@initxy/noeta-web-sdk`
  is published; a change needs a new `ui_api` and a host range that keeps the old one.
- A web part runs with the page's rights (its token, every endpoint): the install confirmation
  and the HTTP install guard are the only checks, as in the projects compared.
- The TUI shows plugins' UI data only, and with ACP and rewind still reads some plugins'
  records by name (the readers in `noeta/toolkit/plugin_records.py`, the todos' records)
  until its code part exists.
- A setup naming a plugin that moved to `noeta-plugins` gets a notice with its install line
  instead of the plugin.
