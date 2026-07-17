/**
 * Template prompt placeholder parsing (ADR-0012).
 * Kept in sync with the backend workflow/templates.py _PLACEHOLDER_RE:
 * {name} is single-line and non-empty; trimmed, deduplicated preserving
 * first-occurrence order.
 */
const PLACEHOLDER_RE = /\{([^{}\n]+)\}/g

export type PromptSegment =
  | { kind: 'text'; text: string }
  | { kind: 'param'; name: string; raw: string }

/** Split a prompt into an alternating sequence of text / placeholder segments (for highlight rendering). */
export function splitPrompt(prompt: string): PromptSegment[] {
  const src = prompt ?? ''
  const out: PromptSegment[] = []
  let last = 0
  for (const m of src.matchAll(PLACEHOLDER_RE)) {
    const idx = m.index ?? 0
    const name = m[1].trim()
    if (!name) continue
    if (idx > last) out.push({ kind: 'text', text: src.slice(last, idx) })
    out.push({ kind: 'param', name, raw: m[0] })
    last = idx + m[0].length
  }
  if (last < src.length) out.push({ kind: 'text', text: src.slice(last) })
  return out
}

/**
 * Replace {param} placeholders in the prompt with parameter values; placeholders
 * without a value are kept verbatim. Must be byte-identical with the backend
 * render_prompt: when starting from a template the frontend uses it to
 * optimistically render the first message, deduplicated against the backend's
 * later user_message event by content equality.
 */
export function renderPrompt(
  prompt: string,
  values: Record<string, string>,
): string {
  return splitPrompt(prompt)
    .map((s) => (s.kind === 'param' ? values[s.name] || s.raw : s.text))
    .join('')
}

/** Placeholder names appearing in the prompt (deduplicated, first-occurrence order). */
export function extractPlaceholders(prompt: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const s of splitPrompt(prompt)) {
    if (s.kind === 'param' && !seen.has(s.name)) {
      seen.add(s.name)
      out.push(s.name)
    }
  }
  return out
}
