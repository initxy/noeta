# chat_bridge — a channel plugin

A chat app posts each message to `POST /chat` as `{delivery_id, thread, sender, text}`; every
thread is one durable task. Only allowlisted senders (by sender id, never by room) get in, and
`yes <id>` / `no <id>` from them answers an approval. The `chat_reply` plugin, inside the task,
posts each answer and approval prompt back to `reply_url` and records what it relayed.

Run it on as many replicas as you like: an inbound delivery lands once (`deliver` creates the
thread's task once and posts each delivery id once), and only the task's one writer relays.

Install: `cp chat_bridge.py ~/.noeta/plugins/`, then in `~/.noeta/config.toml`:

```toml
[channels.chat_bridge]
allow_senders = ["U123"]
reply_url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

`noeta serve` or `noeta worker` starts the listener (port 8788); `chat_reply` joins `main` by
itself. `make example` runs `tests/`.
