/**
 * MCP connector form parsing (pure functions, unit-testable).
 *
 * The connector forms take headers / env / args as plain text (one entry per
 * line) because a dynamic key-value row editor buys little for an
 * operator-facing config surface. These helpers turn that text into the API
 * shapes and report the first malformed line.
 */

export interface ParseResult<T> {
  ok: boolean
  value: T
  /** The first malformed line (verbatim) when !ok. */
  badLine?: string
}

/** Parse "Name: value" lines into a header record. Blank lines are skipped;
 * a line without ":" or with an empty name fails the parse. */
export function parseHeaderLines(text: string): ParseResult<Record<string, string>> {
  return parseKeyValueLines(text, ':')
}

/** Parse "NAME=value" lines into an env record. Blank lines are skipped;
 * a line without "=" or with an empty name fails the parse. */
export function parseEnvLines(text: string): ParseResult<Record<string, string>> {
  return parseKeyValueLines(text, '=')
}

function parseKeyValueLines(
  text: string,
  separator: string,
): ParseResult<Record<string, string>> {
  const value: Record<string, string> = {}
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (!line) continue
    const index = line.indexOf(separator)
    const name = index >= 0 ? line.slice(0, index).trim() : ''
    if (index < 0 || !name) {
      return { ok: false, value: {}, badLine: line }
    }
    value[name] = line.slice(index + 1).trim()
  }
  return { ok: true, value }
}

/** Split an argument line on whitespace ("-y --port 8080" → ["-y", "--port",
 * "8080"]). No quoting rules: an argument containing spaces belongs in a
 * dedicated arg via the API, and the simple form covers the common case. */
export function splitArgs(text: string): string[] {
  return text.split(/\s+/).filter((part) => part.length > 0)
}

/** The SDK's connector alias rule (^[a-z0-9_-]{1,32}$): validated client-side
 * for instant feedback; the backend re-validates authoritatively. */
export function isValidAlias(alias: string): boolean {
  return /^[a-z0-9_-]{1,32}$/.test(alias)
}
