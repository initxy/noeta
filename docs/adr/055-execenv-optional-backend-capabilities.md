# ADR-055: Optional backend capabilities

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-033](033-execution-environment-seam.md) (fs / shell / browser side effects route through an `ExecEnv` + `BrowserBackend` seam, backed by a per-session sandbox container the host provisions). Where the two disagree, this record wins.

## Decision

The Protocol stays as it is — a third-party backend that implements only its
members keeps working — but a backend may now offer two optional capabilities
that tools discover with `getattr`, the way `supports_background` is read:

- **`supports_foreground_kill`** — when true, `run_argv` also takes
  `on_start(kill)`, called once before the backend blocks, with a zero-argument
  `kill` that ends that one command wherever it runs. `Bash` registers `kill` in
  the session's foreground kill table, so interrupt / cancel / close reach a
  command inside a container (see
  [ADR-042](042-interrupt-responsiveness.md) and
  [ADR-058](058-sandbox-foreground-shell-interrupt.md)). The AIO adapter runs every command in its own container shell session
  with `hard_timeout` set, and kills the session through `/v1/shell/kill` on a
  stop or a timeout.
- **`read_range(path, offset, length) -> bytes`** — a byte-exact window,
  raising the same `OSError` subclasses as `read_bytes`. `Read` uses it to pull
  a large file in 1 MiB pieces instead of one whole-file `read_bytes`, whose
  base64 body over HTTP was both slow for a short window and capped by the
  response size limit. The AIO adapter runs `dd` with byte `skip` / `count` behind
  the same not-found / no-permission guards as `read_bytes`. Every other caller of
  `read_bytes` is unchanged.

Both are optional rather than Protocol members because each is a refinement a
backend can do without: without the first, a container command is still bounded
by its timeout; without the second, `Read` still works on the whole body.
