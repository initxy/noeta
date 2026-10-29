# ADR-011: A task tree has at most one holding worker

- Status: accepted
- Refined by: ADR-015
- Date: 2026-10-28
- Refines: ADR-005

## Context

ADR-005 let several workers hold one tree, and a claim skipped every tree held by another
worker. A sub-agent claimed by a second worker before the root's job started could start a job
of its own there: then each worker's hold kept the tree from the other, and no worker could
claim any of its tasks (not even to take a `Cancel`) until a job ended, which for a dev server
is never.

## Decision

- `Store.hold` without an epoch refuses (returns `None`) while another worker holds the tree,
  an expired but unreaped hold included; the check and the insert are one transaction (Postgres
  takes a per-tree advisory lock). `Runtime.hold` reaps once and asks again, then raises
  `HeldElsewhere`.
- What asked for the hold does not run in this process: a background `Bash` or `Monitor` is
  refused with a tool error the model reads, a foreground command past its timeout is killed
  as a plain command is instead of moving to the background, and an MCP server's question is
  declined.

## Rationale

The shell plugin's jobs belong to one process and are found by tree: stopping the root's
turn, ending the root and `TaskStop` all assume the root runs where the jobs are. One holder
keeps that true and keeps a held tree's tasks claimable by a live worker. A refused job is told
to the model; nothing is lost silently.

Rejected: letting any current holder claim (the root could run on a worker without its jobs,
so ending it would leave the other worker's jobs running and `TaskStop` would call them lost);
moving a job to the holding worker (a process cannot move).

## Consequences

- While a sub-agent runs on another worker than its tree's holder, its background commands are
  refused until the holder's jobs end.
- `make cluster` runs a root and its sub-agent on two workers, both starting a long job.
