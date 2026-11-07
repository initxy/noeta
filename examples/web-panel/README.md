# web-panel

A plugin directory with all three parts a plugin can carry today:

- a backend (`__init__.py`): an `Extension` that joins `main` by itself;
- UI data every front end shows: a status item with the conversation's length (`ui`), and a
  call its web part makes, listing the task's directory (`ui_call("workspace")`);
- a web part (`web/`, built to `web/dist/index.js`, named by `.noeta-plugin/plugin.json`): a
  sidebar panel in the web and desktop apps that makes that call.

Install it, then start a new session in the web or desktop app:

```bash
noeta plugin add examples/web-panel
```

Only the built `web/dist/` is served; installing runs no build. To change the web part:

```bash
(cd web && npm ci && npm run build:packages)   # the SDK's types, from noeta's web/
cd examples/web-panel/web && npm ci && npm run typecheck && npm run build
```

`tests/` runs the backend on a scripted model (`make example`).
