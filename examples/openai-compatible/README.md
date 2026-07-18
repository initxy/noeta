# Point the platform at an OpenAI-compatible gateway

By default the platform (`python -m noeta.agent`) runs the offline **mock**
provider. To get real answers, point it at any **OpenAI-Responses-compatible
gateway** — the public OpenAI API, a self-hosted gateway, or any vendor that
speaks the Responses wire shape. Two files are involved:

1. **`apps/noeta-agent/.env`** — the gateway credentials
   ([`.env.example`](./.env.example) here is a filled-in fragment):

   ```dotenv
   LLM_PROVIDER=auto
   LLM_BASE_URL=https://api.openai.com/v1
   LLM_API_KEY=<your-api-key>
   ```

   `LLM_BASE_URL` is the gateway **root** — the provider appends
   `/responses`. `auto` means: use the gateway when both values are set,
   otherwise fall back to the offline mock (so an empty `.env` never breaks
   boot).

2. **`apps/noeta-agent/models.json`** — the model menu users pick from
   ([`models.example.json`](./models.example.json) here is a template). Give
   each entry the gateway's real model `id`, a `label`, reasoning `efforts`
   if the model supports them, and — for models the SDK catalog does not
   know — `context_window` / `max_output_tokens` so context compaction can
   engage.

Then start the platform and verify:

```bash
make run
curl -s http://127.0.0.1:8000/api/v1/health
# {"ok": true, "provider": "openai"}   ← "mock" means credentials didn't take
```

## Two gateways

Models can route to a second Responses-compatible gateway (different host,
`Authorization: Bearer` auth): set `SECONDARY_LLM_BASE_URL` /
`SECONDARY_LLM_API_KEY` and tag the routed models with
`"gateway": "secondary"` in `models.json` (see the second entry in the
template). The secondary only stacks on top of an active primary.

## Security warning

Never commit a real API key. Keep it in `apps/noeta-agent/.env` (gitignored)
or supply it as an environment variable at launch; if a key leaks, rotate it
in your provider's console immediately.

## See also

- [Configure a provider](../../docs/how-to/configure-provider.md)
- [Configuration reference](../../docs/reference/configuration.md) — every key
