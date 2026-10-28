# Claude Code parity tooling

`make parity` measures how what noeta sends to the model differs from what
Claude Code sent in the same scenario. It is offline: noeta talks to a local
fake Messages endpoint that answers with the model turns recorded from Claude
Code. Claude Code is closed source, so nothing of its text enters this
repository: the contract under `parity/<version>/` holds facts (parameters,
tool schemas with descriptions reduced to length + hash, message layouts,
cache-mark positions, text kinds and hashes) and the model's side of each
scenario.

| file | what it does |
|---|---|
| `scenarios.json` | the sessions and the steps typed into each (the single list record, distill and replay use) |
| `project.py` | writes the scenario project (a tiny git repo with a `CLAUDE.md`, a skill, an MCP server, `data.txt`) |
| `record.py` | recording proxy between Claude Code and an upstream Messages API |
| `drive.py` | types a session's steps into Claude Code in tmux, answers prompts with the first choice |
| `distill.py` | recording → `parity/<version>/` (contract + `script/`) |
| `replay.py` | fake endpoint + noeta run per session, then the comparison (`make parity`) |
| `compare.py` | contract vs noeta's capture → `parity/report.md` |
| `facts.py`, `wire.py` | what a request is reduced to; SSE and request kinds |
| `names.json` | our names for text kinds (Claude Code's by hash, noeta's by pattern) |

## Run

    make parity                      # all sessions; parity/report.md, exit 1 on any mismatch
    uv run python -m scripts.parity.replay --session m2 --keep    # one session, keep scratch dir
    uv run python -m scripts.parity.compare                       # re-compare the last capture

`parity/out/` keeps noeta's raw requests of the last run. A mismatch accepted
as a deviation goes into `parity/deviations.md` with its id (the backticked
hash in the report) and the reason.

## Record a new Claude Code version

The baseline is an API-key user on default settings (see the parity spec). A
Mac with Claude Code and tmux, reachable over ssh, records through a gateway:

1. On this machine, start the proxy per session; the token stays here:

        PARITY_UPSTREAM_TOKEN=... python -I scripts/parity/record.py ~/cc-recordings/<ver>/m1 \
            --port 18801 --upstream https://<gateway> --model <cheap-model> --effort medium=high \
            --auth-env PARITY_UPSTREAM_TOKEN
        ssh -N -R 18801:127.0.0.1:18801 mac &

2. Write the project on the Mac (`m3` needs `--data`; later sessions reuse
   the project as earlier ones left it, as the 2.1.312 recording did):

        ssh mac python3 - '~/cc-rec' < scripts/parity/project.py

3. Start Claude Code and drive the session; repeat 1–3 for m2, m3, m4 (m4
   resumes m1's config directory with `--continue`):

        python scripts/parity/drive.py start m1 --ssh mac --tmux /opt/homebrew/bin/tmux \
            --port 18801 --project /Users/<you>/cc-rec/proj
        python scripts/parity/drive.py run m1 --ssh mac --tmux /opt/homebrew/bin/tmux \
            --out ~/cc-recordings/<ver>/m1

4. Check each step's prompt reached the model (a prompt typed while Claude
   Code is busy can be lost) and re-run a session if not; then distill and
   look at the diff of `parity/`:

        uv run python -m scripts.parity.distill ~/cc-recordings/<ver>

Unnamed kinds show as `p:<hash>` in the contract; name the ones that matter
in `names.json` (our words) and distill again.
