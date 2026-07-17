/** Micro-compaction (composer prune) aggregation: micro compactions emit no events —
 *  their only trace is cleared_outputs inside each ContextPlanComposed plan_ref.
 *  The Inspector's compaction section wants "every compaction of the session at a
 *  glance", which means dereferencing every plan: fetch per plan here but keep only
 *  the cleared count (never the body), cached at module level by hash — content is
 *  addressed and immutable, so one fetch per hash across the page; incremental
 *  refreshes / re-entering the page never refetch. Failures are not cached so they
 *  can retry. */
import { useEffect, useState } from 'react'
import { contentUrl } from '../../api/endpoints'
import type { RawEnvelope } from '../../api/types'
import { isContentRef } from './model'

export interface MicroCompaction {
  /** Owning task stream (seq collides across streams; jumps must switch scope first). */
  taskId: string
  /** Seq of the ContextPlanComposed event. */
  seq: number
  occurredAt: number
  /** Number of tool outputs cleared by this compose (length of cleared_outputs). */
  cleared: number
}

const clearedCache = new Map<string, Promise<number>>()

function fetchClearedCount(hash: string): Promise<number> {
  const hit = clearedCache.get(hash)
  if (hit) return hit
  const promise = fetch(contentUrl(hash), { credentials: 'include' }).then(
    async (res) => {
      if (!res.ok) throw new Error(`Failed to load plan (${res.status})`)
      const plan = JSON.parse(await res.text()) as { cleared_outputs?: unknown[] }
      return Array.isArray(plan.cleared_outputs) ? plan.cleared_outputs.length : 0
    },
  )
  promise.catch(() => clearedCache.delete(hash))
  clearedCache.set(hash, promise)
  return promise
}

/** Every compose in the event stream where a micro compaction happened
 *  (cleared > 0), sorted by occurrence time. Dereferencing is async: the first
 *  render returns [], then everything lands at once when all plans resolve. */
export function useMicroCompactions(events: RawEnvelope[]): MicroCompaction[] {
  const [list, setList] = useState<MicroCompaction[]>([])

  useEffect(() => {
    let alive = true
    const plans = events.filter((ev) => ev.type === 'ContextPlanComposed')
    if (plans.length === 0) {
      setList([])
      return
    }
    void Promise.all(
      plans.map(async (ev): Promise<MicroCompaction | null> => {
        const payload = ev.payload as Record<string, unknown> | null
        const ref = payload?.plan_ref
        if (!isContentRef(ref)) return null
        try {
          const cleared = await fetchClearedCount(ref.hash)
          if (cleared <= 0) return null
          return { taskId: ev.task_id, seq: ev.seq, occurredAt: ev.occurred_at, cleared }
        } catch {
          return null
        }
      }),
    ).then((resolved) => {
      if (!alive) return
      setList(
        resolved
          .filter((m): m is MicroCompaction => m !== null)
          .sort((a, b) => a.occurredAt - b.occurredAt),
      )
    })
    return () => {
      alive = false
    }
  }, [events])

  return list
}
