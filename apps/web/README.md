# noeta-web

Standalone frontend project for the Noeta coding app. It depends only on the
Noeta HTTP/SSE contract and has two product pages:

- `/chat` — session list, transcript, approvals, composer.
- `/trace?task={id}` — raw event timeline, payload/content deref, context view.

Root `/` is served by the Python host as a redirect to `/chat`.

## Contract

- `GET /events` — global Server-Sent Events stream of canonical
  `EventEnvelope` projections.
- `GET /tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/events`,
  `GET /tasks/{id}/artifacts/{hash}`,
  `GET /tasks/{id}/messages/{hash}`,
  `GET /tasks/{id}/content/{hash}`,
  `GET /tasks/{id}/context` — read models and task-scoped content.
- `POST /tasks`, `.../goals`, `.../approvals`, `.../answers`, `.../cancel`,
  `.../close`, `.../reopen` — chat commands.
- `GET /capabilities` — feature probe.

## Source Layout

- `chat.html`, `trace.html` — Vite multi-page entry documents.
- `src/pages/chat/`, `src/pages/trace/` — thin React page entrypoints.
- `src/app/` — chat and trace React applications + data hooks.
- `src/components/ai-elements/` — local AI Elements-style primitives adapted
  from the ai-elements registry model.
- `src/domain/` — pure EventEnvelope projections (`reducer.js`, event merge).
- `src/shared/` — browser-independent shared helpers.
- `src/styles/app.css` — shared application styling.

## Commands

```bash
npm install
npm run build
npm run dev
```

`npm run build` writes `dist/`. The Python host serves the built Vite files
from `dist/`; browsers cannot execute the React source JSX or bare module
imports directly. During frontend development use `npm run dev`.
