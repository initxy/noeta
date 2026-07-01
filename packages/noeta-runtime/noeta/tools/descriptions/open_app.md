Render an interactive HTML app from the workspace in the right-hand "App" panel, with a same-origin proxy so its `/api/*` calls reach an external service without CORS.

## What it does

Mounts a workspace directory (containing `index.html` plus any JS/CSS) as a live web app served from a local preview gateway, and opens it in the UI's right-side "App" tab. The gateway also forwards every `/api/<path>` request the page makes to `proxy_to/<path>` on the server side, so the page can talk to an external API as if it were same-origin (no browser CORS).

## When to use

When a result is complex or interactive enough that a rendered, clickable page beats plain text or a static file — dashboards, filterable tables, charts, comparison views, or a small control panel that drives an external HTTP API.

## When NOT to use

- For a plain answer, a code snippet, or a one-off file the user just needs to read — write a file or answer in text instead.
- When the page needs credentials/auth to reach the API: this v1 forwards with no credential injection (intended for local, unauthenticated demo targets only).

## Preconditions

- `dir` is a workspace-relative directory you have already written, and it contains an `index.html` entry point.
- Inside the page, call the external API via relative `/api/...` URLs (NOT the absolute target URL), so requests stay same-origin and get proxied.
- `proxy_to` is the base URL of the target service (e.g. `http://localhost:3000`); `/api/users` is forwarded to `proxy_to/users`.
- Drive interactions from JavaScript `fetch`, not native form submission: use `<button type="button">` handlers, or call `event.preventDefault()` in a `submit` listener. The page renders in a sandboxed iframe, so a real form submit would reload/navigate the page and lose state.
