# Skill directories: a tiered merge across ecosystem conventions, opt-in beyond the workspace

## Context

Noeta's skill format is already ecosystem-compatible: `SKILL.md` bodies with
Claude Code frontmatter, honored where it gates behavior, and — because the
model-visible tool names mirror Claude Code's — a foreign skill's
`allowed-tools` maps onto Noeta's tool vocabulary nearly verbatim. The
coding-agent ecosystem has meanwhile converged on directory conventions: a
vendor-neutral `.agents/skills` (workspace and `~/`), per-product directories
(`~/.claude/skills`, `~/.codex/skills`), and the norm — set by Pi, which the
convention grew around — that no agent auto-scans another product's directory;
foreign directories are an explicit settings opt-in. The question this file
answers: which directories does Noeta index, in what precedence, and on whose
authority.

## Decision

**One overlay-wins merge over an ordered tier list** (low → high):

```
built-in  <  plugin-contributed  <  extra_skill_dirs  <  ~/.agents/skills  <  ~/.noeta/skills  <  <workspace>/.agents/skills  <  <workspace>/.noeta/skills
```

Two rules produce the order — a **wider scope sits below a narrower scope**,
and **at equal scope the vendor-neutral directory sits below the
vendor-specific one** — so `.noeta/skills` keeps sovereignty: a same-named
skill in the Noeta-native directory always wins.

**Only the two workspace tiers mount by default.** Home-scoped tiers
(`~/.agents/skills`, `~/.noeta/skills`) and borrowed foreign directories
(`extra_skill_dirs`, e.g. `~/.claude/skills`) are host/operator opt-in. A
server-side SDK must not silently read the operating user's home directory,
and it never scans another product's configuration directory uninvited.

**An operator `skills_dir` override pins the workspace-scoped set.** When the
host names its own directory for the workspace tier, no repo-derived tier —
`.agents/skills` included — mounts beneath it: a host that pins the skill
catalogue keeps exact control over what a session sees.

**The workspace tiers can be gated on the plugin trust store.**
`workspace_skills_trust` is `"open"` (default) or `"trust-store"`; any other
value raises at session build — a typo on a security knob must not read as
"open". The gate's subject is the HOST-side workspace path (`trust_subject`),
never the pack's own `workspace_dir`: in sandbox mode that is a container
mount point every session shares, so keying on it would collapse per-repo
trust into a global on/off. The store is the same one that gates workspace
plugin directories — one `grant_trust` covers both. An untrusted workspace
skips exactly the repo-derived tiers actually in play, with a warning that
carries the subject and the skipped directories as attributes; when nothing
repo-derived would mount, there is no stake and no warning.

**Skill identity is the frontmatter `name`, never the directory name.** The
agent-skills convention wants the two to match; shared and borrowed
directories are exactly where they diverge, so Noeta reads only the
frontmatter. A file without `name` is skipped with a log.

**Workspace instruction files follow the same posture**: the search order is
`NOETA.md` → `AGENTS.md` → `CLAUDE.md`, first non-empty wins, for the root
file and for read-triggered subdirectory discovery alike. A repo carrying
both AGENTS.md and CLAUDE.md picks AGENTS.md — such repos conventionally make
CLAUDE.md an `@`-include of AGENTS.md, and loading both would double the
content.

## Rationale

- **Harvest the ecosystem where the conventions are already neutral, stay
  polite where they are not.** `.agents/skills` and `AGENTS.md` are
  cross-vendor property; reading them by default costs nothing and gains every
  repo that adopted them. `~/.claude/skills` is another product's territory:
  its skills may lean on that product's machinery, so ingesting it is the
  operator's call, one config line away — the same split the rest of the
  ecosystem settled on.
- **Workspace content is repo content.** A cloned repository can carry
  `.agents/skills` written by strangers; the trust gate exists so a hardened
  host can require an explicit grant before repo-authored skills (and their
  `allowed-tools` requests) enter the menu. Both workspace tiers stand or
  fall together — gating only the new directory would claim a security
  property the old one silently bypasses.
- **Deliberate opt-ins outrank shipped defaults.** Within the lowest band the
  order built-in < plugin < `extra_skill_dirs` mirrors the global tiers
  beating built-ins: a directory the operator named by hand expresses intent
  the shipped catalogue cannot.

## Alternatives considered

- **Auto-scan `~/.claude/skills` (or any home directory) by default** —
  rejected: surprises the operating user, breaks test hermeticity, and reads
  another product's configuration without invitation. Pi itself makes these a
  settings opt-in.
- **First-class `HostConfig` fields for each new directory** — rejected: the
  per-plugin config channel already reaches the pack; a knob with a single
  consumer stays in the plugin's own entry.
- **Directory-name fallback when frontmatter `name` is missing** — rejected:
  two identity rules are worse than one; ecosystem skills carry `name` in
  practice.
- **Gating only the new `.agents/skills` tier on trust** — rejected as
  incoherent; see Rationale.
- **Indexing Pi's `.pi/skills`** — rejected: the same sovereignty argument in
  reverse; vendor-private directories belong to their vendor.
- **`.mcp.json` discovery from the workspace** — deliberately not decided
  here: auto-connecting MCP servers from repo config needs its own
  trust-and-credential design.

## Consequences

- The trust store is a file outside the workspace: a grant or revocation
  changes the tier set — and with it the skill-menu bytes in the composed
  stable prefix — on the next session build. An interactive host that wants a
  per-session prompt calls `is_trusted` itself rather than listening for the
  (per-process-deduplicated) warning.
- A borrowed directory can shadow a shipped skill of the same name. That is
  the deliberate opt-in reading; hosts that must not allow it simply do not
  configure `extra_skill_dirs`.
- The skill-menu budget operates on the merged registry, so a large borrowed
  directory competes for menu space under the existing mechanism rather than
  bypassing it.
