# Claude Code parity tooling

`make parity` measures how what noeta sends to the model differs from what
Claude Code sent in the same scenario. It is offline: noeta talks to a local
fake Messages endpoint that answers with the model turns recorded from Claude
Code. Claude Code is closed source, so its prose stays out of this
repository: the contract under `parity/<version>/` holds interface facts
(parameters, tool schemas with descriptions reduced to length + hash, message
layouts, cache-mark positions, text kinds and hashes) and the model's side of
each scenario. What exactly is committed is listed under
[Committed derived data](#committed-derived-data).

The recordings themselves live outside the repository, in
`$NOETA_PARITY_RECORDINGS` (default `~/cc-recordings`), one directory per
recording: `<version>/` and re-recordings `<version>-r<n>/`, each holding
`<session>/*.json`.

| file | what it does |
|---|---|
| `scenarios.json` | the sessions and the steps typed into each (the single list record, distill and replay use) |
| `project.py` | writes the scenario project (a tiny git repo with a `CLAUDE.md`, a skill, an MCP server, `data.txt`); noeta's copy has the same files in its own places (`NOETA.md`, `.noeta/`) |
| `record.py` | recording proxy between Claude Code and an upstream Messages API |
| `drive.py` | types a session's steps into Claude Code in tmux, answers prompts with the first choice |
| `distill.py` | recording → `parity/<version>/` (contract + `script/`) |
| `replay.py` | fake endpoint + noeta run per session, then the comparison (`make parity`) |
| `compare.py` | contract vs noeta's capture → `parity/report.md` |
| `facts.py`, `wire.py` | what a request is reduced to; SSE and request kinds |
| `names.json` | our names for text kinds (Claude Code's by hash, noeta's by pattern) |
| `own_words.py` | an advisory report of where noeta's texts repeat a run of 45+ characters of Claude Code's recorded texts (`make parity`; always exits 0, ADR-020); it hashes the recordings in memory and skips with a notice when there are none (CI); `parity/own-words-allowed.txt` lists the protocol literals it skips |

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

1. Record each session (sessions on different ports can run at once; m4
   resumes m1 and runs after it). `drive.py session` writes a fresh project
   and config under `--root/<session>` on the Mac, starts the proxy here and
   an ssh tunnel to it, drives every step, and stops it all; the upstream
   token stays on this machine (`PARITY_UPSTREAM_TOKEN`):

        PARITY_UPSTREAM_TOKEN=... python -u scripts/parity/drive.py session m1 --ssh mac \
            --tmux /opt/homebrew/bin/tmux --port 18811 --root /Users/<you>/cc-rec/r2 \
            --out-root ~/cc-recordings/<ver> --upstream https://<gateway> --model <cheap-model>

   Pick an upstream model that answers in seconds and accepts every request
   shape Claude Code sends: the auto-mode classifier's first stage sends
   `thinking: {type: disabled}` with `max_tokens` 64, which some gateways turn
   into a reasoning setting their model rejects (`--drop-disabled-thinking`
   forwards it without the field; the recording keeps Claude Code's body and
   notes each rewrite in `upstream_rewrites`).

2. Read `<session>.marks`: a `!!!` line is a step that did not go as
   scripted (a prompt that started no request, a card that never showed).
   `<session>.cards/` holds each card and the key that answered it. Re-run
   the session if needed; then distill and look at the diff of `parity/`:

        uv run python -m scripts.parity.distill ~/cc-recordings/<ver> --out parity/<ver>

Unnamed kinds show as `p:<hash>` in the contract; name the ones that matter
in `names.json` (our words) and distill again.

The own-words report picks the new recording up by its directory name once
`parity/<ver>/` exists; nothing of it is hashed into the repository.

## Committed derived data

Derived from Claude Code 2.1.312's recorded requests and committed, so that
`make parity` runs from a clone. Each item is an interface fact a client must
match to speak the same protocol to the model, not prose, except the model
replies in `script/`:

- `parity/<version>/m*.json`: per request, the parameter keys and values
  (`max_tokens`, `thinking`, `context_management`, `safeguards` types and
  classifier-context names), the beta header values and HTTP header *names*,
  message layouts, cache-mark positions, and text kinds with hashes. Verbatim
  because a byte-for-byte protocol match is the point.
- `parity/<version>/tools.json`: each tool's name and input schema (property
  names, types, enums, `required`) verbatim; every description is reduced to
  its length and a 10-hex-digit hash of the whole text.
- `parity/<version>/texts.json`: per text kind, lengths and whole-text hashes.
  A whole-text hash is reversible only by guessing the whole text, so it
  discloses only short texts such as a bare tag.
- `parity/<version>/script/`: the model's side of each scenario (replies of
  the upstream model, with the steps we typed). The replies are kept as the
  model wrote them, so they can paraphrase or quote Claude Code's prompts (a
  compaction summary quotes the summary request's instructions, for one);
  ADR-020 allows that where it serves parity. A thinking block keeps its
  place but its text is replaced by a placeholder.
- `parity/rules/*.md` and `parity/rules/tools/*.md` (one file per tool the
  recorded sessions send, the MCP server's tools sharing one, 21 files):
  Claude Code's rules restated in our words, with
  sha256 hashes of the source texts (no-copy rule in `parity/rules/README.md`,
  which also indexes them). `parity/rules/source_hash.py` computes those
  hashes from the recordings; it holds no recorded text.
- `parity/deviations.md`: the accepted model-visible differences, each with
  its mismatch id and reason, in our words.
- `parity/own-words-allowed.txt`: literals that both sides must spell the
  same way (tags, sentinels, formats), each with its reason.

Not committed: the recordings, and any index of hashed text windows (even
unkeyed short-window hashes can be reversed from a known opening, a few
characters at a time; `.gitignore` keeps `parity/*/cc-windows.bin` out).

Whether interface facts copied from a closed-source client may be published
is a licensing call: the owner signs off on this list before the repository
is made public.
