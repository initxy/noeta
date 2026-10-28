# ADR-008: The sandbox keeps code-running paths read-only and covers missing ones with placeholders

- Status: accepted
- Date: 2026-10-28

## Context

With the sandbox on, `Bash` may write anywhere in the working directory without asking. Some
paths there are run or read as configuration later, outside the sandbox and without a person's
confirmation: noeta's own `.noeta/` (settings, hooks, plugins), git's hooks and config, and the
settings package managers and build tools execute code from. A sandboxed command that writes
one of them escapes the sandbox on the next ordinary command. Making existing ones read-only is
not enough: a command can create a missing git pre-commit hook or `.npmrc`.

## Decision

- The protected paths (`noeta/toolkit/protected_paths.py`; also git's hooks and config,
  submodules' configs and hooks, and `core.hooksPath`) are read-only inside the sandbox.
- Those that would be executed on the next ordinary command without a confirmation step are
  covered, when missing, by an empty read-only placeholder for the command's lifetime, so they
  cannot be created either. Names that run only after a separate step of the person's
  (`direnv allow`, trusting a workspace in an editor) are read-only when present, without a
  placeholder.
- No directory above a protected path can be renamed away.
- The sandbox itself is Claude Code's `sandbox` settings, implemented with bubblewrap on Linux
  and Seatbelt on macOS (`noeta/toolkit/sandbox.py`).

## Rationale

The sandbox's promise is that what runs inside cannot act outside it. A write that is executed
later by an unsandboxed process breaks that promise without any escape exploit, so the
protected set is "what runs code later without asking", and both existing and missing paths
have to be covered. Placeholders are the only way to forbid creating a file without forbidding
writes to its whole directory.

Rejected: protecting only existing files (creation is the common attack); making the whole
working directory read-only (defeats the sandbox's purpose); asking before every write
(defeats auto-approval of sandboxed commands).

## Consequences

- Inside the sandbox, `git init` fails in a directory that is not a repository, and nothing
  under `.noeta/` can be written by a command (the permission rules still let `Edit` / `Write`
  change `.noeta/skills` and `.noeta/commands`).
- Placeholders are hidden from git when the working directory is a repository's top.
- A newly found code-running path is added to the protected set.
