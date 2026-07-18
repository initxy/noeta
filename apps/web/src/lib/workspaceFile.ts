/**
 * Session-workspace file-reference matching: file-looking text in conversation
 * bodies → workspace-relative paths.
 *
 * Lets Markdown render matching inline code / relative links as file chips
 * (click opens the side-panel preview). Pure function, unit-testable.
 */

/**
 * Build a matcher function from the list of workspace file paths. Returns the
 * workspace-relative path on a hit, null otherwise.
 *
 * Rules:
 * - An exact full relative path match has the highest priority;
 * - A bare filename (basename) matches only when unique across the workspace —
 *   duplicated names are ambiguous, never guessed;
 * - `./` and `/workspace/` (in-container absolute form) prefixes are allowed.
 */
export function buildWorkspaceFileMatcher(
  paths: string[],
): (text: string) => string | null {
  const byPath = new Set(paths)
  // basename → path; duplicates set to null to mark ambiguity
  const byName = new Map<string, string | null>()
  for (const p of paths) {
    const name = p.slice(p.lastIndexOf('/') + 1)
    byName.set(name, byName.has(name) ? null : p)
  }
  return (text: string) => {
    const t = text.trim().replace(/^(\.\/|\/workspace\/)/, '')
    if (!t) return null
    if (byPath.has(t)) return t
    return byName.get(t) ?? null
  }
}
