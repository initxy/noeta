# oss_skills fixture provenance

Fixtures proving Noeta loads real open-source Skill directories unchanged
(Phase 4.5 I5). This file and the sibling `LICENSE` live **outside**
every skill root on purpose, so skill-resource discovery never picks
them up as a skill's resource.

Each skill root is `oss_skills/<name>/` and contains a `SKILL.md`.

## Verbatim, unmodified — real public skills

Copied byte-for-byte from the `claude-plugins-official` marketplace,
source repo `https://github.com/42Crunch-AI/claude-plugins.git`
(**Apache-2.0**; the full license text is in `oss_skills/LICENSE`,
identical to the upstream repo's root LICENSE).

| fixture | upstream subpath | files (all verbatim) |
| --- | --- | --- |
| `example-command/` | `plugins/example-plugin/skills/example-command/` | `SKILL.md` |
| `session-report/` | `plugins/session-report/skills/session-report/` | `SKILL.md`, `analyze-sessions.mjs`, `template.html` |

* `example-command` — frontmatter carries the real-world break I5
  fixes: `argument-hint` (hyphenated key), `allowed-tools: [Read, Glob,
  Grep, Bash]` (inline list literal captured as an opaque string), and
  documents a `model` override. Single-file skill.
* `session-report` — its `SKILL.md` body references the two bundled
  files (`analyze-sessions.mjs`, `template.html`) by name; I5 records
  them as `resources` for audit but does **not** execute the script or
  inline the template.

These two are **literal public content** — do not edit them. If they
need refreshing, re-copy from the upstream subpaths above.

## Authored for test — NOT public content

| fixture | files | author |
| --- | --- | --- |
| `refactor-guide/` | `SKILL.md`, `DEEPENING.md`, `PATTERNS.md`, `scripts/check.sh` | written for the Noeta test suite |

`refactor-guide` is **original content authored for this repository**,
not copied from any public skill. It gives a controlled
progressive-disclosure case (multiple `.md` references + a bundled
script) with a known-exact resource list, so the determinism and
resource-discovery assertions are pinned to fixed bytes. It also
exercises non-semantic frontmatter keys (`license`,
`disable-model-invocation`) flowing into `SkillDescription.metadata`.
