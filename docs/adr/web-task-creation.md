# Web task creation: the request carries only a model selector, provider config stays server-side, and an interactive session finishes on a trailing suspend

## Context

The web frontend needs to be able to create sessions, but the web server holds credentials on behalf of many Principals — a real trust boundary — so the request body must not decide provider / credentials / tools. At the same time, an interactive session finishing "normally" must not fabricate a terminal state.

This decision draws two lines: what a web task-creation request does and does not carry; and the durable finishing form of an interactive session. Naming layers evolve later along with library-sdk-architecture.md, but the trust boundary and the selector mechanism do not change.

## Decision

### Web can create tasks, but the request carries a selector, not a config

- **Web can create tasks**: `POST /tasks` and every turn's `send_goal` go through the **same** `InteractionDriver.start` / `send_goal` seam that the CLI uses. There is **exactly one** code path to create a task — writing `TaskCreated` from server-side config — with **no** HTTP-only side path that assembles task config from the request body.
- **The request carries a model *selector*, not a provider *config***: the request body may include a model selector (like `"opus"/"sonnet"/"haiku"`), and the server resolves it against **`Principal.allowed_models` ∩ the deployment allowlist** into a concrete provider binding. The server **never** reads from the request body: a provider object, `base_url`, an API key / credentials, `profile`, or the tool-registry.
- **The trust boundary is per-surface, the driver is shared**: the CLI runs on the user's own machine with their own credentials → no trust boundary → free to pick provider / model / key. The web server holds credentials for many Principals → a real trust boundary → it must validate the selector and reject a bare provider config. Both call the same driver; the only difference is input validation in the transport adapter.
- **The durable home of the selector and its authorization binding is a new typed event** (`ModelBound {model, principal_identity}`, written by the Engine, once at task start + once for each per-turn switch), **not** a new field on `TaskCreated`. A new event type is simply absent from old recordings (zero byte drift), and a per-turn switch wouldn't fit into the immutable `TaskCreated` anyway. `Principal` is a tiny L0 value `{identity, allowed_models}`.

### An interactive session finishes on a trailing suspend, not a terminal state

- Interactive turns run `final=False`. The multi-turn wrapper only rewrites a `FinishDecision` into a next-goal `TaskSuspended(wake_on=HumanResponseReceived)`; `fail` / `cancel` / `approval` / `subtask` / `timer` keep their own native semantics. **A normally-completed interactive turn finishes on a trailing next-goal suspend** — that (not a terminal state) is the session's durable termination state. "Exiting" is just no longer sending a goal; the Task can still be resumed.
- Only `run` (one-shot) has a single goal known to be final → `TaskCompleted`.
- **"Closing a session" does not fabricate a `TaskCompleted` from the control plane** (that would create a terminal state with no source policy Decision, violating the single-writer invariant). It uses a new typed event `ConversationClosed` (written by the Engine, not touching `task.status`, folded into `GovernanceState.closed`). "closed" is advisory, not a lock — continuing the chat reopens it.

## Rationale

- **A selector + allowlist gives the user a choice without reopening a config leak.** Letting the request body decide provider / model / profile / tool-registry is a config-leak / abuse vector on an unauthenticated surface (arbitrary `base_url`, smuggled credentials, unauthorized tools). But "just forbid task creation" is too broad — a Claude-style web UI's very first action is "start a new conversation." Narrowing the red line to "reject config, allow selection" satisfies both.
- **A shared driver, with the difference locked to adapter validation, prevents two runtime code paths.** Creating a task goes through `InteractionDriver` just like send_goal / approve / cancel; no surface invents its own runtime logic. CLI and web differ only in the strictness of input validation.
- **An interactive session not creating a terminal state protects the single-writer invariant and "a Task can be durably resumed."** A terminal state must come from some policy Decision; a control-plane `TaskCompleted` would fabricate a Decision-less termination state. Stopping a normal completion at a next-goal suspend keeps a Task always `--continue`-able / web-reopenable. "closed" cannot be an Observer marker (Observers are best-effort projections, not truth) nor a second `TaskState` writer (breaking single-writer), so it uses a new typed event + a fold query.

## Alternatives considered

1. **Keep "web can't create tasks."** Rejected: it directly defeats the product goal; the web UI degrades into a read-only dashboard.
2. **Receive a full provider / model config from the request body.** Rejected: it reopens the config leak (arbitrary `base_url`, smuggled credentials, unauthorized tool-registry). A selector + allowlist gives model selection without reopening it.
3. **Express "closed" via a control-plane `TaskCompleted` / use an Observer marker as the closed truth / open a second `TaskState` writer for closed.** Rejected: fabricating a Decision-less terminal state breaks single-writer; an Observer is a best-effort projection and can't be state of record; a second writer breaks single-writer. Instead use a new `ConversationClosed` event + a fold query, so the EventLog stays the single source of truth.

## Consequences

- The shared `InteractionDriver.start` / `send_goal` seam lands in `noeta.execution.driver`; per-surface input validation and selector resolution land in the product host layer's transport adapter.
- `ModelBound` / `ConversationClosed` events land in `noeta.protocols.events`; `Principal {identity, allowed_models}` lands in `noeta.protocols.values`; the `FinishDecision` → next-goal suspend rewrite lands in the multi-turn wrapper.
- Constraint: the web surface only ever accepts selectors, and credentials / provider config can only come from server-side config. When adding a model capability, sync it into the intersection of `Principal.allowed_models` and the deployment allowlist, or selector resolution will reject it.
