/**
 * Knowledge-base citations: parse citation footnotes in AI replies + collect
 * consulted paths from tool calls.
 *
 * Wire protocol (see CONTEXT.md "citations" / ADR-0013): the model emits GFM
 * footnote conventions — `[^n]` in the body, definition lines at the end:
 * `[^n]: knowledge/<source name>/<path>[#<heading text>]`. Structured citations
 * are never persisted: the frontend parses them from the message text on the
 * fly and fills in title / origin link / excerpt via the resolve-paths
 * endpoint. Pure functions, unit-testable.
 */

/** Internal href prefix of citation chips: protocol footnote references are
 * rewritten to [n](#cite-n) and recognized by the Markdown `a` component via
 * this prefix. */
export const CITE_HREF_PREFIX = '#cite-'

export interface CitationDef {
  /** Footnote label text (the `1` of `[^1]`). */
  label: string
  /** Normalized path (anchor stripped), always starting with knowledge/. */
  path: string
  /** Heading anchor text; null for whole-document citations. */
  anchor: string | null
  /** The raw path[#anchor] text; input for the resolve request. */
  raw: string
}

/** Protocol footnote definition line: `[^n]: knowledge/...` (line start, path
 * without whitespace). Factory function: a /g regex carries mutable lastIndex
 * state, so it is never shared at module level. */
const defLineRe = () => /^\[\^([^\]\s]+)\]:[ \t]+(knowledge\/\S+)[ \t]*$/gm

/** Parse the protocol footnote definitions in one assistant message. Ordinary
 * footnotes with non-knowledge/ paths are excluded (kept for remark-gfm's
 * default rendering). */
export function parseCitationDefs(text: string): CitationDef[] {
  const defs: CitationDef[] = []
  const seen = new Set<string>()
  for (const m of text.matchAll(defLineRe())) {
    const label = m[1]
    if (seen.has(label)) continue
    seen.add(label)
    const raw = m[2]
    const hash = raw.indexOf('#')
    defs.push({
      label,
      path: hash < 0 ? raw : raw.slice(0, hash),
      anchor: hash < 0 ? null : raw.slice(hash + 1) || null,
      raw,
    })
  }
  return defs
}

/**
 * Rewrite protocol footnotes into a form interceptable by the Markdown `a`
 * component:
 * - protocol definition lines are removed (ordinary footnote definitions stay);
 * - `[^n]` references (only labels present in defs) → `[n](#cite-n)`.
 * Returns the text unchanged when defs is empty.
 */
export function rewriteCitationMarkup(text: string, defs: CitationDef[]): string {
  if (defs.length === 0) return text
  const labels = new Set(defs.map((d) => d.label))
  let out = text.replace(defLineRe(), (line, label: string) =>
    labels.has(label) ? '' : line,
  )
  out = out.replace(/\[\^([^\]\s]+)\]/g, (whole, label: string) =>
    labels.has(label)
      ? `[${label}](${CITE_HREF_PREFIX}${encodeURIComponent(label)})`
      : whole,
  )
  // Removing definition lines can leave runs of blank lines; squash to at most one.
  return out.replace(/\n{3,}/g, '\n\n').trimEnd()
}

/** Recover the footnote label from a #cite-n href; returns null for non-citation hrefs. */
export function citeLabelFromHref(href: string | undefined): string | null {
  if (!href || !href.startsWith(CITE_HREF_PREFIX)) return null
  try {
    return decodeURIComponent(href.slice(CITE_HREF_PREFIX.length))
  } catch {
    return null
  }
}

/** knowledge/ file paths explicitly appearing in a shell_run command string
 * (extension required, to avoid picking up directory arguments). CJK and Latin
 * quotes/brackets/punctuation act as boundaries. */
const SHELL_PATH_RE =
  /knowledge\/[^\s'"`“”‘’()（）\[\]<>|;，。；：]+\.[A-Za-z0-9]+/g

/**
 * Extract consulted knowledge/ paths from one tool call's arguments (D8
 * semantics, best-effort):
 * - read: take the path argument directly;
 * - shell_run: regex-extract explicit paths from the command string (files
 *   discovered from rg output are missed; false negatives accepted).
 */
export function extractKnowledgePaths(toolName: string, args: unknown): string[] {
  if (args == null || typeof args !== 'object') return []
  const rec = args as Record<string, unknown>
  if (toolName === 'read') {
    const p = rec.path
    if (typeof p === 'string') {
      const t = p.trim().replace(/^(\.\/|\/workspace\/)/, '')
      if (t.startsWith('knowledge/')) return [t]
    }
    return []
  }
  if (toolName === 'shell_run') {
    const cmd = rec.command
    if (typeof cmd !== 'string') return []
    return [...cmd.matchAll(SHELL_PATH_RE)].map((m) => m[0])
  }
  return []
}
