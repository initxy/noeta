/** Deref ContentStore content by hash: a module-level LRU cache (content-addressed
 *  and immutable — one fetch per hash for the whole page); failures are not cached
 *  so they can retry.
 *
 *  The cache is bounded: dual caps on entry count and total decoded characters;
 *  overflow evicts from the oldest (Map iteration order is insertion order; a hit
 *  does delete+set to move to the tail — LRU). Eviction only affects the cache
 *  itself — bodies already resolved into components stay; re-expanding refetches. */
import { useEffect, useState } from 'react'
import { contentUrl } from '../../api/endpoints'

const MAX_ENTRIES = 64
// Decoded-character budget (about 32MB of memory in UTF-16); a single file over
// budget is also evicted — it just never stays cached, without affecting the
// current display.
const MAX_TOTAL_CHARS = 16 * 1024 * 1024

interface CacheEntry {
  promise: Promise<string>
  /** Written as body.length once resolved; 0 while pending (not counted toward the budget). */
  chars: number
}

const bodyCache = new Map<string, CacheEntry>()
let totalChars = 0

function evict(): void {
  for (const [hash, entry] of bodyCache) {
    if (bodyCache.size <= MAX_ENTRIES && totalChars <= MAX_TOTAL_CHARS) return
    bodyCache.delete(hash)
    totalChars -= entry.chars
  }
}

function drop(hash: string): void {
  const entry = bodyCache.get(hash)
  if (entry) {
    bodyCache.delete(hash)
    totalChars -= entry.chars
  }
}

function fetchBody(hash: string): Promise<string> {
  const hit = bodyCache.get(hash)
  if (hit) {
    // LRU touch: move to the tail of the insertion order.
    bodyCache.delete(hash)
    bodyCache.set(hash, hit)
    return hit.promise
  }
  const entry: CacheEntry = {
    promise: fetch(contentUrl(hash), { credentials: 'include' }).then(async (res) => {
      if (!res.ok) throw new Error(res.status === 404 ? 'Content not found' : `Failed to load content (${res.status})`)
      return res.text()
    }),
    chars: 0,
  }
  entry.promise.then(
    (body) => {
      // It may have been evicted/replaced while pending; only count this entry
      // toward the budget if it is still the one in the cache.
      if (bodyCache.get(hash) === entry) {
        entry.chars = body.length
        totalChars += body.length
        evict()
      }
    },
    () => drop(hash),
  )
  bodyCache.set(hash, entry)
  evict()
  return entry.promise
}

export interface ContentBodyState {
  body: string | null
  loading: boolean
  error: string | null
}

/** No request when hash is null (lazy deref: fetch only when a chip expands). */
export function useContentBody(hash: string | null): ContentBodyState {
  const [state, setState] = useState<ContentBodyState>({
    body: null,
    loading: hash != null,
    error: null,
  })

  useEffect(() => {
    if (!hash) {
      setState({ body: null, loading: false, error: null })
      return
    }
    let alive = true
    setState({ body: null, loading: true, error: null })
    fetchBody(hash).then(
      (body) => alive && setState({ body, loading: false, error: null }),
      (e: Error) => alive && setState({ body: null, loading: false, error: e.message }),
    )
    return () => {
      alive = false
    }
  }, [hash])

  return state
}
