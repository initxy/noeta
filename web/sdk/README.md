# @noeta-ai/web-sdk

What a noeta plugin's web part is given, and where it can put things. The web app and the
desktop app load the part of every plugin the open session runs; the guide is
[Write a plugin › A web part](../../docs/guides/plugins.md#a-web-part).

A web part is one built ES module exporting `activate(ctx)`:

```tsx
import { SLOTS, type PluginContext } from "@noeta-ai/web-sdk";

export function activate(ctx: PluginContext) {
  return ctx.slots.add(SLOTS.sidebar, { key: "hello", mount: "keyed" }, () => <div>Task {ctx.client.taskId}</div>);
}
```

`ctx` holds `client` (read, watch, send, answer, signal for the session's task), `records(kind)`
and `allRecords()` (the plugin's own records; the hooks `useRecords` and `useAllRecords` keep a
component current), `call(name, args)` (its backend's `ui_call`), `slots` (`add`, `wait`),
`theme`, `desktop` (the desktop app's bridge, `null` in a browser) and `Transcript` (the page's
view of another task's conversation, such as a sub-agent's). The slots and the props each
passes are `SLOTS`, `toolSlot` (`ToolSlotProps`: the call, and whether its card is open),
`requestSlot` (`RequestSlotProps`: an approval or question about a call), `recordSlot` and
`codeblockSlot`; `statusbar` passes `StatusbarSlotProps` (how full the context is),
`session.header` `SessionHeaderSlotProps` (the listed title). In a `list`, a negative `order`
comes before the slot's own content. `activate` may be async; a rejection shows on the
plugins page. What it returns (or resolves to) is called when the plugin is switched off for
the session, its web part is rebuilt (a new entry hash), or the page leaves it; whatever it
added to slots goes then too, what it subscribed to through `ctx` (`client.watch`,
`theme.onChange`) is unsubscribed, and that `ctx` adds nothing more.

## Building

The page provides `react`, `react-dom`, `react-dom/client`, `react/jsx-runtime`,
`@noeta-ai/web-sdk` and `@noeta-ai/ui` itself (its import map), so a build marks them
external and bundles everything else. `template/` is a starting point: `npm install`, then
`npm run build` writes `dist/index.js`. The plugin's `.noeta-plugin/plugin.json` names it:

```json
{ "name": "my-plugin", "version": "0.1.0", "ui_api": 1, "web": "web/dist/index.js" }
```

Commit the built `dist/`: installing a plugin runs no build. `examples/web-panel` in noeta's
repository is a whole plugin built this way.
