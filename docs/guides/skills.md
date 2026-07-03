# Writing a custom skill

A skill is a local, static LLM-workflow template stored at
`.noeta/skills/<name>/SKILL.md`. It has YAML frontmatter (`name`,
`description`, optional `version` / `priority`) plus a Markdown body.
Any sibling files are bundled as on-demand resources the model can `read`.

## Directory structure

```
.noeta/skills/
  pdf-extract/
    SKILL.md              # required: frontmatter + body
    references/
      pdf-spec.md         # on-demand resource
    scripts/
      extract.py          # on-demand resource
  code-review/
    SKILL.md
```

Three-layer merge (low → high priority):

1. **Built-in** skills shipped with Noeta
2. **Global** skills at `~/.noeta/skills/`
3. **Workspace** skills at `<workspace>/.noeta/skills/`

A skill with the same `name` at a higher layer overrides the lower one.

## SKILL.md format

```markdown
---
name: pdf-extract
description: Extract text and tables from PDF files.
version: "1"
priority: 100
---

# PDF Extraction Workflow

When the user asks to extract content from a PDF:

1. Use `read` to check if the file exists.
2. Run `scripts/extract.py` via `shell_run`.
3. Summarize the extracted text.

Base directory for this skill: <absolute path>
```

### Frontmatter fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `name` | **yes** | — | Stable identifier. Must match `^[a-z0-9][a-z0-9-]*$` (lowercase letters, digits, hyphens). Used in the model's `skill` control tool. |
| `description` | **yes** | — | One-line summary shown in the skill menu the model sees. |
| `version` | no | `"1"` | Recorded for schema evolution; not used for filtering. |
| `priority` | no | `100` | Render order when multiple skills are active (ascending). Lower = rendered first. |

Unknown frontmatter keys are tolerated and stored as metadata — they
never cause a parse failure. So real public skills carrying extra keys
like `allowed-tools` or `argument-hint` load without errors.

### Body

The Markdown body is appended verbatim into the semi-stable View segment
when the skill is activated. No eager inlining of resources happens —
the model uses `read` to pull them on demand.

The `Base directory for this skill:` line is injected automatically by
the runtime so the model can resolve relative references (e.g.
`references/pdf-spec.md`) to absolute paths.

## How activation works

Skills are activated in **two stages**, both model-driven:

### Stage 1: Menu rendering

At session start, the skill indexer scans all three layers and builds a
registry of `name → description`. This registry is rendered into the
`skill` control tool's schema so the model sees a menu like:

```
Available skills:
  pdf-extract — Extract text and tables from PDF files.
  code-review — Review code for bugs and style.
```

No skill body is loaded at this point — just names and descriptions.

### Stage 2: Body loading

When the model calls the `skill` control tool with a name (e.g.
`skill: pdf-extract`), that skill's body + base-directory line are
folded into the next turn's semi-stable context. The model can then
`read` bundled resources by absolute path.

This two-stage design keeps the initial context lean while still
letting the model discover and activate specialized workflows on
demand.

## Example: a "research" skill

```markdown
---
name: research
description: Search the web, read sources, synthesize findings.
version: "1"
---

# Research Workflow

Follow this process when asked to research a topic:

1. Use `web_search` to find relevant sources.
2. Use `webfetch` to read at least 3 sources.
3. Cross-reference claims across sources.
4. Write a structured summary using `write`.

## Quality bar

- Cite URLs for every claim.
- Flag conflicting information.
- Prefer primary sources over secondary.
```

Place it at `.noeta/skills/research/SKILL.md` in your workspace. On
next session start, it appears in the skill menu automatically — no
registration step needed.

## Key points

- **`SKILL.md` is the manifest.** The file name is fixed; the directory
  name is the skill's identity on disk (though `name` in frontmatter is
  the canonical key).
- **No hot-reload.** Skills are indexed at session start. Edit a skill,
  start a new session — the updated version loads.
- **Resources are on-demand.** Sibling files are never inlined. The
  model reads them explicitly via the `read` tool, same as any workspace
  file.
- **Skills ≠ Tools.** A tool is a callable function the model invokes.
  A skill is a Markdown workflow template that guides *which* tools to
  use and in what order. See [Glossary](../reference/glossary.md#skill).

## Source

- Skill indexer: `packages/noeta-runtime/noeta/context/skills/indexer.py`
- Frontmatter parser: `packages/noeta-runtime/noeta/context/skills/_frontmatter.py`
- See also: [ADR: Model-driven skill invocation](../adr/model-driven-skill-invocation.md),
  [ADR: Skill resource on-demand](../adr/skill-resource-on-demand.md)
