# chat_bridge — a channel plugin

A chat app posts each message to `POST /chat` as `{delivery_id, thread, sender, text}`; every
thread is one durable task. Only allowlisted senders (by sender id, never by room) get in, and
`yes <id>` / `no <id>` from them answers an approval. The `chat_reply` plugin, inside the task,
posts each answer and approval prompt back to its `url` and records what it relayed. The task's
config holds only the thread id, so the reply URL (and any token in it) never enters the task log.

Run it on as many replicas as you like: an inbound delivery lands once (`deliver` creates the
thread's task once and posts each delivery id once), and only the task's one writer relays.

Install: `cp chat_bridge.py ~/.noeta/plugins/`, then in `~/.noeta/config.toml`:

```toml
[channels.chat_bridge]
allow_senders = ["U123"]

[plugins.chat_reply]
url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

`noeta serve` or `noeta worker` starts the listener (port 8788); `chat_reply` joins `main` by
itself. `make example` runs `tests/`.
