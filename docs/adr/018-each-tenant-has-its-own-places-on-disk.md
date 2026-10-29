# ADR-018: Each tenant has its own places on disk, and a workspace it must be given

- Status: superseded by ADR-028
- Refined by: [ADR-021](021-a-tenant-reads-all-but-a-protected-set-and-writes-only-its-own-places.md)
  (reads all but a protected set; writes only the tenant's own places)
- Date: 2026-10-28
- Refines: [ADR-014](014-tenants-separated-at-the-api-tools-need-an-os-boundary.md)

## Context

ADR-014 fences a non-default tenant's file tools and commands off from noeta's home and other
tenants' workspaces, keeping the task's own corners, among them `projects/<slug>`. Three gaps
remained: tenants working in the same project shared `projects/<slug>` (auto memory and
compaction transcripts); everything a task wrote to the temp directory (background job logs,
a sandboxed command's `TMPDIR`, proxy sockets, a PDF read's scratch copies) sat in places
every tenant could see; and a tenant without a workspace ran in the server's directory, next
to the default tenant's work.

## Decision

- **Project directories by tenant.** A non-default tenant's memory and compaction transcripts
  live in `projects/<tenant>/<slug>/` (a slug starts with `-`, a tenant name never does); the
  default tenant's stay in `projects/<slug>/` (`noeta/toolkit/paths.py`). In it a task's own
  corners are `memory/` and, to read, `<task id>.jsonl` and `<root id>.jsonl`.
- **A temp directory per tenant.** Each non-default tenant has
  `<temp>/noeta-temp-<uid>/tenants/<tenant>/` (0700; a long or unusual name is cut and
  hashed so proxy socket paths stay short), holding its job logs (`jobs/`), its sandboxed
  commands' `TMPDIR` (`tmp/`), proxy sockets and scratch copies. The fence refuses, and the
  tenant's confined profile hides, the rest of `noeta-temp-<uid>` and
  `noeta-background-<uid>` in every temp directory a command may use; worker sandbox
  directories live in `noeta-temp-<uid>/sandbox/`.
- **A workspace is required.** `noeta serve` refuses to start while a non-default tenant it
  knows of has no `workspace`, `noeta token add` refuses such a tenant, and the HTTP API
  answers 403 to its task creation and to any `cwd` (a token added while serving, host code).
  A `cwd` whose closest workspace is another tenant's is refused, nested workspaces included.

## Rationale

Each place a task writes is either one only its tenant reaches or one it cannot reach at all;
a place shared by name (a project slug, `/tmp`) is shared in fact. Requiring a workspace
makes the operator choose where a tenant's work lives instead of noeta defaulting to the
server's own directory.

Rejected: a temp directory per task (nothing would clean them, and a tenant's tasks already
share its plans); temp files under the spill directory (kept forever, and hidden from the
tenant); flat `/tmp` names matched by a glob (one made while a command runs would be
visible); deriving a default workspace (a directory the operator never chose, and a new place
to keep on every worker).

## Consequences

- A root task does not read its sub-agents' compaction transcripts (the fence does not know
  the tree's ids).
- Adding a tenant needs a workspace on every worker that runs its tasks; with a shared
  filesystem, put workspaces there.
