# chat_approvals — approving tool calls from a chat

Goes with [`chat-bridge`](../chat-bridge): while one of its tasks waits on an approval, the
`chat_approval_prompt` plugin posts `{thread, approvals: [{call_id, tool, input}]}` to its `url`
(at least once, with an `Idempotency-Key` header `<task id>:<seq>`), and the chat app shows Allow
and Deny buttons. A press comes back to `POST /approve` as `{thread, call_id, sender, verdict}`
(`verdict` is `allow` or `deny`) with the shared `secret` in `X-Chat-Secret`: 401 without the
secret, 400 for a malformed body, 403 for a sender not on the allowlist. The thread names the
task, so a press can only answer a chat task's call, and the answer is posted once however many
replicas receive it.

Install: `cp chat_approvals.py ~/.noeta/plugins/` next to `chat_bridge.py`, then in
`~/.noeta/config.toml`:

```toml
[channels.chat_approvals]
secret = "{env:CHAT_BRIDGE_SECRET}"
allow_senders = ["U123"]

[plugins.chat_approval_prompt]
url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

`noeta serve` or `noeta worker` starts the listener (port 8789); `chat_approval_prompt` joins
`main` by itself. `make example` runs `tests/`.
