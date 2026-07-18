# Deploying the platform

**You don't need containers to run noeta-agent.** The platform is a single
process: `uv` + a writable data directory is a complete deployment —

```bash
make install && make run          # or: uv sync && uv run python -m noeta.agent
```

— with all state under `DATA_DIR` (SQLite app DB, engine event log, session
workspaces). Docker only enters the picture for the **per-session sandbox**
(and even then the app itself can stay on the host). This directory is
**optional packaging** for teams that standardize on compose.

## Compose, zero-credential mode

```bash
cd examples/deployment
docker compose up --build
# → http://127.0.0.1:8000 — dev-login with any username, mock LLM, sandbox off
```

The [`Dockerfile`](./Dockerfile) builds the SPA and the backend into one
image; [`docker-compose.yml`](./docker-compose.yml) runs it with a named
volume for `/data`. Uncomment the environment lines to wire a real
OpenAI-Responses-compatible gateway (`LLM_BASE_URL` / `LLM_API_KEY` — see
[`../openai-compatible/`](../openai-compatible/)) and an admin allowlist.

## Enabling the sandbox under compose

The sandbox provisions **one Docker container per session** by shelling out
to `docker`. From inside a container that means Docker-outside-of-Docker:
the app talks to the **host's** daemon through the mounted socket, and
sandbox containers become siblings of the app container. Three consequences
you must configure around:

1. **Mount the socket** — uncomment the `/var/run/docker.sock` volume line.
   (A DinD sidecar is *not* a drop-in alternative: the provider reaches
   sandbox containers via ports published on `127.0.0.1`, which a separate
   DinD daemon's network namespace would not share.)
2. **Use host networking** — sandbox ports are published on the host's
   loopback, so the app must share the host's network namespace to reach
   them: replace the `ports:` mapping with `network_mode: host`.
3. **Bind-mount `DATA_DIR` at an identical host path** — the provider
   bind-mounts session workspaces into sandbox containers, and with a
   socket-mounted setup those bind paths are resolved by the **host**
   daemon. A named volume breaks that; use a real host directory mounted at
   the same absolute path, e.g.:

   ```yaml
   services:
     noeta:
       network_mode: host          # replaces ports:
       environment:
         DATA_DIR: /srv/noeta-data
         SHARED_DATA_DIR: /srv/noeta-data/shared
         SANDBOX_ENABLED: "true"
         SANDBOX_API_KEY: ${SANDBOX_API_KEY}   # container auth, e.g. openssl rand -hex 16
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock
         - /srv/noeta-data:/srv/noeta-data
   ```

If that reads like more trouble than it's worth: it usually is. The
straightforward sandbox deployment is the app **bare on the host**
(`SANDBOX_ENABLED=true` in `apps/noeta-agent/.env`) with Docker running
locally — the container machinery then just works.

## What to change for any real deployment

- `SESSION_SECRET` — signs the login cookie; never keep the dev default.
- `ADMIN_USERS` — who gets the admin console.
- **Auth** — dev-login accepts any username; front the server with your own
  identity by implementing the `AuthProvider` seam
  (`apps/noeta-agent/noeta/agent/auth/provider.py`), and disable dev-login.
- **TLS** — put a reverse proxy in front and set
  `SESSION_COOKIE_SECURE=true`.
- Mind the v1 limits: single process, single instance; no rate limiting or
  quotas. See the
  [platform reference](../../docs/reference/noeta-agent.md#honest-limits-v1).
