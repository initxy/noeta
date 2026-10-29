# chat_bridge — a channel plugin

A chat app posts each message to `POST /chat` as `{delivery_id, thread, sender, text}`, with the
shared `secret` in the `X-Chat-Secret` header; every thread is one durable task (once that task
ended, `deliver` continues the thread in a new task that remembers it). A request without
the secret is refused (401) before its sender id is believed, a body without those four strings
too (400); then only allowlisted senders (by sender id, never by room) get in. The `chat_reply`
plugin, inside the task, posts each answer back to its `url` and records what it relayed: at
least once, since a crash between the post and the record posts again, with the same
`Idempotency-Key` header (`<task id>:<seq>`) so the chat app can drop the repeat. The task's
config holds only the thread id, so the reply URL (and any token in it) never enters the task
log. To approve tool calls from the chat as well, add [`chat-approvals`](../chat-approvals).

Run it on as many replicas as you like: an inbound delivery lands once (`deliver` creates the
thread's task once and posts each delivery id once), and only the task's one writer relays.

Install: `cp chat_bridge.py ~/.noeta/plugins/`, then in `~/.noeta/config.toml`:

```toml
[channels.chat_bridge]
secret = "{env:CHAT_BRIDGE_SECRET}"
allow_senders = ["U123"]

[plugins.chat_reply]
url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

`noeta serve` or `noeta worker` starts the listener (port 8788); `chat_reply` joins `main` by
itself. `make example` runs `tests/`.
