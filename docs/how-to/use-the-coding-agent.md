# Use the platform

**Goal:** drive the noeta-agent platform for real work — log in, set up a
space (knowledge, skills, MCP connectors, agent-config), hold sessions, and
use the files panel and sandbox preview.

**Before you start:** the platform is running (`make run`, or
`python -m noeta.agent`; see the [quickstart](../tutorials/quickstart.md)).
Everything below works in zero-credential mock mode; for real answers wire a
gateway first ([connect a gateway](configure-provider.md)).

## 1. Log in

Open the server URL (default <http://127.0.0.1:8000>). The default build
uses **dev-login**: enter any username and you are in. Every user gets a
**personal space** automatically; usernames listed in `ADMIN_USERS` also see
the admin console.

> Dev-login is a development affordance. A real deployment plugs an identity
> provider into the `AuthProvider` seam and disables dev-login (hot-
> switchable from the admin console).

## 2. Pick or create a space

The space switcher lists your spaces. Your personal space is yours alone;
**team spaces** are shared — create one, then add members (owners manage
membership, members use the space). Everything the agent brings to a session
is scoped to the current space.

## 3. Give the space material

All of this is optional — a bare space chats fine — but it is what makes the
agent *yours*:

- **Knowledge** (space page → Knowledge): add a `git_repo` source (clone
  URL, optional token) or a `local_dir` source, then trigger a sync. Synced
  content is mounted read-only into session sandboxes and the agent cites it
  back to source paths.
- **Skills** (space page → Skills): upload a skill (a `SKILL.md` pack, zip
  or single file). The model activates skills on demand from its skill menu;
  platform-wide builtin skills are managed by admins.
- **MCP connectors** (space page → MCP): register an MCP server under an
  alias — `http` (URL + headers) or `stdio` (command + args + env) — then
  optionally restrict it to a tool subset and enable it. Enabled connectors
  are resolved into the agent every turn; their tools appear as
  `mcp__<alias>__<tool>`. Credentials stay server-side.
- **Agent-config** (space page → Agent): a persona prompt (written into each
  session workspace as `AGENT.md`), the default model and reasoning effort
  for new sessions, which knowledge sources take part, and the memory
  toggle.
- **Templates**: reusable prompts with typed parameters, and multi-node
  **workflow templates** chaining them.

## 4. Hold a session

Click **New session**, type a message, pick a model / reasoning effort in
the composer if you want to override the space default. During a turn you
see streamed assistant text and thinking, tool calls with results, todo-list
updates, skill activations, and subtask cards — all replayable: reload the
page mid-turn and the stream re-derives from the event log.

- **Questions** — the agent can pause on a structured question (choices +
  freeform); the session waits until you answer.
- **Stop** — cancels the running turn; the partial history stays recorded.
- **Images** — attach PNG / JPEG / GIF / WebP (≤ 5 MB each) in the composer;
  they ride the turn to vision-capable models.
- **Templates / workflows** — start a session from a template (parameters →
  first message) or a workflow template; a workflow session shows one tab
  per node and advances through a generated handoff you review and confirm.
- **Feedback** — rate any assistant message; space owners can later run the
  analysis agent over collected ratings and adopt its suggestions.

Sessions are listed per space; any space member can open a team-space
session, while deleting is limited to the creator or a space owner.

## 5. Files panel and sandbox preview

With the sandbox on (`SANDBOX_ENABLED=true` + Docker), each session runs in
its own container and the right dock comes alive:

- **Files** — the session workspace (everything the agent wrote), listed and
  readable from the host-side mount; agent output lands here.
- **Preview panels** — live **Browser**, **Terminal**, and **Code** views
  streamed from the session's container (served on a separate preview
  origin; discovery is automatic).

Without Docker the platform runs in pure conversation mode: no file surface,
no shell execution — everything else above still works.

## See also

- [Platform reference](../reference/noeta-agent.md) — architecture and boot modes
- [HTTP API reference](../reference/http-api.md) — the routes behind the UI
- [Connect an OpenAI-compatible gateway](configure-provider.md)
- [Connect MCP servers](connect-mcp.md)
