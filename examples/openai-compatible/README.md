# Launch noeta-agent against a real OpenAI-compatible model

This directory ships a **ready-to-use config file** [`config.json`](./config.json) plus the launch commands. It points the official coding agent (`python -m noeta.agent`) at a real OpenAI-compatible model instead of the offline stub.

Any OpenAI-compatible endpoint works — the public OpenAI API, a self-hosted gateway, or any vendor that speaks the Chat Completions wire shape. Fill in your own `base_url` / `api_key` / `model`.

## Background: two env-var naming schemes, don't mix them

The repo has two conventions that are easy to confuse:

| Purpose | Reader | Variable prefix |
| --- | --- | --- |
| **Launch the official code agent** (`python -m noeta.agent`) | `noeta.agent.backend.lifecycle.BackendConfig` | `NOETA_AGENT_*`, or a `NOETA_AGENT_CONFIG` JSON file |
| **Run the demo scripts under examples** (`real_provider_*_demo.py`, etc.) | each script itself | `NOETA_OPENAI_*` / `NOETA_PROVIDER` |

Your `NOETA_OPENAI_BASE_URL / NOETA_OPENAI_API_KEY / NOETA_OPENAI_MODEL / NOETA_PROVIDER` strings are the **second** scheme (for demo scripts). To launch the actual code agent, use the first. Both methods below use the first scheme; pick either one.

Field mapping:

| What you have (demo naming) | Launcher field (`config.json` / `NOETA_AGENT_*`) |
| --- | --- |
| `NOETA_PROVIDER=openai` | `provider_id` / `NOETA_AGENT_PROVIDER` |
| `NOETA_OPENAI_MODEL` | `model` / `NOETA_AGENT_MODEL` |
| `NOETA_OPENAI_BASE_URL` | `base_url` / `NOETA_AGENT_BASE_URL` |
| `NOETA_OPENAI_API_KEY` | `api_key` / `NOETA_AGENT_API_KEY` |

## Method 1: use a config file (recommended)

First fill in [`config.json`](./config.json) with your real `model` / `base_url` / `api_key` (the shipped values are placeholders). Then point `NOETA_AGENT_CONFIG` at it and the launcher reads the keys from it. Even the workspace is baked into the config file (`workspace_dir` field), so the command line needs **just one variable**:

```bash
uv pip install -e apps/noeta-agent          # first-time install (pulls in noeta-sdk + noeta-runtime)

NOETA_AGENT_CONFIG=examples/openai-compatible/config.json python -m noeta.agent
```

> `python -m noeta.agent` is "a launcher, not a CLI": it takes **no positional args or `--flags`**, so all config goes through env vars or the config file. Putting config and workspace "on the command line" means the `NOETA_AGENT_*=… python -m noeta.agent` prefix form above — and once the workspace is folded into the config file, only `NOETA_AGENT_CONFIG` is left.

- `workspace_dir` in `config.json` points at the project directory the agent reads and writes (the directory must exist). The default is `"."` (the directory you run the command from); an absolute path to your project is safer. You can also **omit** the field and pass it on the command line: `NOETA_AGENT_CONFIG=… NOETA_AGENT_WORKSPACE=./my-project python -m noeta.agent`.
- On startup the console prints `noeta.agent serving at http://127.0.0.1:<port>/`; open that address in a browser for the chat UI. `Ctrl-C` to quit.
- To persist sessions, change `sqlite_path` in `config.json` from `":memory:"` to a file path (e.g. `./session.sqlite`).

### Precedence

Low → high: dataclass defaults < `NOETA_AGENT_CONFIG` file < `NOETA_AGENT_*` env vars. So to override one value from the file, just prepend a `NOETA_AGENT_*` to the command instead of editing the file. For example:

```bash
NOETA_AGENT_CONFIG=examples/openai-compatible/config.json \
NOETA_AGENT_MODEL=<another-model-id> \
python -m noeta.agent
```

## Method 2: env vars only (no file)

If you already have those `export` lines in your shell, just rename them to the `NOETA_AGENT_*` names the launcher understands:

```bash
export NOETA_AGENT_PROVIDER=openai
export NOETA_AGENT_BASE_URL=https://api.openai.com/v1
export NOETA_AGENT_API_KEY=<your-api-key>
export NOETA_AGENT_MODEL=<your-model-id>
export NOETA_AGENT_WORKSPACE=./my-project

python -m noeta.agent
```

> Note: `export NOETA_OPENAI_*` / `NOETA_PROVIDER` has **no effect** on `python -m noeta.agent` — only the demo scripts under examples read those.

## Security warning ⚠️

Never commit a real API key. The shipped `config.json` uses a `<your-api-key>` placeholder; keep your real key local only:

- Don't push `config.json` with a real key to a public repo. Add it to `.gitignore`, or keep the `<your-api-key>` placeholder and supply the real key via a `NOETA_AGENT_API_KEY` env var at launch.
- If a key ever leaks, rotate it in your provider's console as soon as possible.
