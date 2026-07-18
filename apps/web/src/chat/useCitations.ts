/**
 * Citation-path resolution hook: hand the knowledge/ paths collected in a
 * session to the resolve-paths endpoint in batches, returning a map of raw
 * path → structured result.
 *
 * Module-level cache (keyed by spaceId|raw): each path resolves once per page
 * lifetime; excerpt drift after a knowledge-source re-sync is accepted (a page
 * refresh picks up the new value, ADR-0013). Failures are not cached (retried
 * on the next render); malformed paths (model-fabricated ../ etc.) are filtered
 * on the frontend first so one bad path cannot 422 a whole batch.
 */
import { useEffect, useMemo, useState } from 'react'
import { knowledgeApi } from '../api/endpoints'
import type { ResolvedKnowledgePath } from '../api/types'

const cache = new Map<string, ResolvedKnowledgePath>()
const pending = new Map<string, Promise<unknown>>()

/** Per-batch cap (matches the backend MAX_PATHS). */
const BATCH = 50

/** Path shape pre-check: aligned with the backend parse_citation_path rejection surface. */
export function isValidCitationRaw(raw: string): boolean {
  if (raw.length > 512 || !raw.startsWith('knowledge/')) return false
  const pathPart = raw.split('#', 1)[0]
  if (pathPart.includes('\\')) return false
  const parts = pathPart.slice('knowledge/'.length).split('/').filter(
    (p) => p !== '' && p !== '.',
  )
  return parts.length >= 2 && !parts.includes('..')
}

/**
 * Resolve a set of citation paths (raw may carry a #anchor). Returns
 * Map<raw, result>; unresolved entries are absent from the Map (callers render
 * them as pending). Always returns an empty Map when spaceId is empty or the
 * list is empty.
 */
export function useResolvedKnowledgePaths(
  spaceId: string | null | undefined,
  raws: string[],
): Map<string, ResolvedKnowledgePath> {
  const [version, setVersion] = useState(0)
  // The array is a fresh reference every render; use a content key as the dependency.
  const key = `${spaceId ?? ''}\x00${raws.join('\x00')}`

  useEffect(() => {
    if (!spaceId || raws.length === 0) return
    let alive = true
    const waits: Promise<unknown>[] = []
    const missing: string[] = []
    for (const raw of new Set(raws)) {
      if (!isValidCitationRaw(raw)) continue
      const k = `${spaceId}\x00${raw}`
      if (cache.has(k)) continue
      const inflight = pending.get(k)
      if (inflight) waits.push(inflight)
      else missing.push(raw)
    }
    for (let i = 0; i < missing.length; i += BATCH) {
      const chunk = missing.slice(i, i + BATCH)
      const prom = knowledgeApi
        .resolvePaths(spaceId, chunk)
        .then((r) => {
          r.items.forEach((item, j) => {
            cache.set(`${spaceId}\x00${chunk[j]}`, item)
          })
        })
        .catch(() => {
          // Failures are not cached: leave them for the next render to retry.
        })
        .finally(() => {
          for (const raw of chunk) pending.delete(`${spaceId}\x00${raw}`)
        })
      for (const raw of chunk) pending.set(`${spaceId}\x00${raw}`, prom)
      waits.push(prom)
    }
    if (waits.length > 0) {
      Promise.allSettled(waits).then(() => {
        if (alive) setVersion((v) => v + 1)
      })
    }
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- key covers spaceId+raws
  }, [key])

  return useMemo(() => {
    const out = new Map<string, ResolvedKnowledgePath>()
    if (!spaceId) return out
    for (const raw of raws) {
      const hit = cache.get(`${spaceId}\x00${raw}`)
      if (hit) out.set(raw, hit)
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps -- key covers spaceId+raws; version drives cache refresh
  }, [key, version])
}
