import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { adminApi } from '../api/endpoints'
import type { RawEnvelope, Session } from '../api/types'
import { cn } from '../lib/cn'
import { IconChevron, IconRefresh, IconSearch } from './icons'
import { Inspector } from './trace/Inspector'
import {
  CATEGORY_COLORS,
  categoryOf,
  clock,
  collectTaskExecution,
  compactionKindOf,
  compactionLabel,
  eventSubagentId,
  fmtDuration,
  fmtTokens,
  groupByTurn,
  isDrawerType,
  subagentLabel,
  summaryOf,
  turnStats,
  type SubagentStatus,
  type SubagentTrace,
  type TaskExecutionTrace,
  type TurnGroup,
} from './trace/model'
import { JsonTree } from './trace/RefChip'
import { TraceSummary } from './trace/TraceSummary'
import { TurnView } from './trace/TurnView'

const ORIGINS = ['all', 'engine', 'llm', 'tool', 'observer', 'system'] as const

function matches(ev: RawEnvelope, query: string): boolean {
  if (!query) return true
  const q = query.toLowerCase()
  if (ev.type.toLowerCase().includes(q)) return true
  if (ev.actor.toLowerCase().includes(q)) return true
  try {
    return JSON.stringify(ev.payload).toLowerCase().includes(q)
  } catch {
    return false
  }
}

// ---- Center column: non-LLM event detail (structured header + payload JsonTree + raw JSON) ----

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-[12px]">
      <span className="w-24 shrink-0 font-mono text-[11px] text-ink-3">{label}</span>
      <span className="min-w-0 break-all font-mono text-[11.5px] text-ink-2">{value}</span>
    </div>
  )
}

function EventDetail({ event }: { event: RawEnvelope }) {
  const [showRaw, setShowRaw] = useState(false)
  const isCompaction = event.type === 'CompactionRequested' || event.type === 'Compacted'
  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 space-y-1.5 border-b border-border px-4 py-3">
        <p className="font-mono text-[13px] font-medium text-ink">
          #{event.seq} {event.type}
          <span className={cn('ml-2 text-[10.5px]', CATEGORY_COLORS[categoryOf(event.type)])}>
            {categoryOf(event.type)}
          </span>
        </p>
        <DetailField label="id" value={event.id} />
        <DetailField
          label="occurred_at"
          value={`${clock(event.occurred_at)} (${event.occurred_at.toFixed(3)})`}
        />
        <DetailField label="actor" value={event.actor} />
        <DetailField label="origin" value={event.origin} />
        <DetailField label="trace_id" value={event.trace_id} />
        {event.causation_id && <DetailField label="causation_id" value={event.causation_id} />}
        {isCompaction && (
          <DetailField label="compaction" value={compactionLabel(compactionKindOf(event))} />
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <p className="mb-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
          payload
        </p>
        <div className="rounded-lg bg-surface-2 p-3">
          <JsonTree value={event.payload} />
        </div>
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          className="mt-3 font-mono text-[10.5px] text-ink-3 hover:text-ink"
        >
          {showRaw ? 'Hide raw JSON' : 'Show raw JSON'}
        </button>
        {showRaw && (
          <pre className="mt-1.5 overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
            {JSON.stringify(event.payload, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}

// ---- Left column: the timeline ----

function EventRow({
  ev,
  selected,
  delta,
  owner,
  dimmed,
  onSelect,
}: {
  ev: RawEnvelope
  selected: boolean
  delta: number | null
  owner?: string
  dimmed?: boolean
  onSelect: (seq: number) => void
}) {
  const summary = summaryOf(ev)
  const compaction =
    ev.type === 'CompactionRequested' || ev.type === 'Compacted'
      ? compactionLabel(compactionKindOf(ev))
      : ''
  return (
    <button
      type="button"
      onClick={() => onSelect(ev.seq)}
      className={cn(
        'flex w-full items-start gap-2 rounded-md py-1 pl-6 pr-2 text-left transition-colors',
        selected ? 'bg-accent-soft' : compaction ? 'bg-warn-soft/60 hover:bg-warn-soft' : 'hover:bg-surface-2',
        dimmed && 'opacity-60',
      )}
    >
      <span className="w-7 shrink-0 pt-px text-right font-mono text-[10px] text-ink-3">
        {ev.seq}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline justify-between gap-1.5">
          <span
            className={cn(
              'min-w-0 truncate font-mono text-[11.5px]',
              CATEGORY_COLORS[categoryOf(ev.type)],
            )}
          >
            {ev.type}
          </span>
          <span className="shrink-0 font-mono text-[9.5px] text-ink-3">
            {clock(ev.occurred_at)}
          </span>
        </span>
        {(owner || compaction) && (
          <span className="mt-0.5 flex flex-wrap gap-1">
            {owner && (
              <span className="rounded-full border border-border bg-surface px-1.5 py-0.5 font-mono text-[9.5px] text-ink-3">
                {owner}
              </span>
            )}
            {compaction && (
              <span className="rounded-full border border-warn/25 bg-warn-soft px-1.5 py-0.5 font-mono text-[9.5px] text-warn">
                {compaction}
              </span>
            )}
          </span>
        )}
        {(summary || delta != null) && (
          <span className="flex items-baseline justify-between gap-1.5">
            <span className="min-w-0 truncate text-[10.5px] text-ink-3" title={summary}>
              {summary}
            </span>
            {delta != null && (
              <span className="shrink-0 font-mono text-[9.5px] text-ink-3">
                +{delta.toFixed(2)}s
              </span>
            )}
          </span>
        )}
      </span>
    </button>
  )
}

function statusText(status: SubagentStatus | string): string {
  switch (status) {
    case 'running':
      return 'running'
    case 'completed':
      return 'done'
    case 'failed':
      return 'failed'
    case 'cancelled':
      return 'cancelled'
    case 'unknown':
      return 'unknown'
    default:
      return status || 'unknown'
  }
}

/** Seq of the first LLM round within a task's events (falling back to the first
 *  event's seq, then null). taskId=null means no task filtering (the first turn of
 *  the whole stream). Used when switching scope to pick the center column's default
 *  selection, so TurnView lands directly on that scope's first turn. */
function firstTurnSeq(events: RawEnvelope[], taskId: string | null): number | null {
  let firstLlm: number | null = null
  let first: number | null = null
  for (const ev of events) {
    if (taskId && ev.task_id !== taskId) continue
    if (first === null) first = ev.seq
    if (firstLlm === null && ev.type === 'LLMRequestStarted') firstLlm = ev.seq
  }
  return firstLlm ?? first
}

function statusClass(status: SubagentStatus | string): string {
  if (status === 'running') return 'text-accent'
  if (status === 'failed' || status === 'cancelled') return 'text-danger'
  if (status === 'completed') return 'text-ink-2'
  return 'text-ink-3'
}

function ExecutionTree({
  trace,
  activeTaskId,
  onSelect,
}: {
  trace: TaskExecutionTrace
  activeTaskId: string | null
  onSelect: (taskId: string | null) => void
}) {
  if (!trace.mainTaskId && trace.subagents.length === 0) return null
  // activeTaskId === null: the default main view (the root stream); otherwise a subagent's task_id.
  const mainActive = activeTaskId === null
  return (
    <div className="rounded-lg border border-border bg-bg p-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
          execution
        </span>
        <span className="font-mono text-[10px] text-ink-3">subagents · {trace.subagents.length}</span>
      </div>
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={cn(
          'flex w-full items-start gap-2 rounded-md px-1.5 py-1 text-left transition-colors',
          mainActive ? 'bg-accent-soft' : 'hover:bg-surface-2',
        )}
      >
        <span className="mt-1 h-2 w-2 rounded-full bg-accent" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center justify-between gap-2">
            <span className="truncate text-[12px] font-medium text-ink">main</span>
            <span className={cn('font-mono text-[10px]', statusClass(trace.mainStatus))}>
              {statusText(trace.mainStatus)}
            </span>
          </span>
          <span className="block truncate font-mono text-[10px] text-ink-3" title={trace.mainTaskId}>
            {trace.mainTaskId || '—'} · {trace.mainEventCount} events
          </span>
        </span>
      </button>
      {trace.subagents.length > 0 && (
        <div className="ml-2 border-l border-border pl-2">
          {trace.subagents.map((sub) => (
            <SubagentTreeRow
              key={sub.id}
              subagent={sub}
              active={activeTaskId === sub.id}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function SubagentTreeRow({
  subagent,
  active,
  onSelect,
}: {
  subagent: SubagentTrace
  active: boolean
  onSelect: (taskId: string | null) => void
}) {
  const content = (
    <>
      <span className="mt-1 h-2 w-2 rounded-full bg-surface-3 ring-1 ring-border" />
      <span className="min-w-0 flex-1">
        <span className="flex items-center justify-between gap-2">
          <span className="truncate text-[11.5px] font-medium text-ink" title={subagent.goal}>
            {subagentLabel(subagent)}
          </span>
          <span className={cn('font-mono text-[10px]', statusClass(subagent.status))}>
            {statusText(subagent.status)}
          </span>
        </span>
        <span className="block truncate text-[10.5px] text-ink-3" title={subagent.goal || subagent.summary}>
          {subagent.goal || subagent.summary || subagent.id}
        </span>
        <span className="block font-mono text-[10px] text-ink-3">
          seq {subagent.startSeq ?? '—'} → {subagent.endSeq ?? '…'} · {subagent.eventCount} events
        </span>
      </span>
    </>
  )
  return (
    <button
      type="button"
      onClick={() => onSelect(subagent.id)}
      className={cn(
        'flex w-full items-start gap-2 rounded-md px-1.5 py-1 text-left transition-colors',
        active ? 'bg-accent-soft' : 'hover:bg-surface-2',
      )}
    >
      {content}
    </button>
  )
}

function TurnHeaderInfo({ group }: { group: TurnGroup }) {
  const stats = turnStats(group)
  if (!stats) return null
  const parts = [
    stats.model,
    `${fmtTokens(stats.tokensIn)}→${fmtTokens(stats.tokensOut)} tok`,
    ...(stats.costUsd > 0 ? [`$${stats.costUsd.toFixed(4)}`] : []),
    fmtDuration(stats.durationS),
  ]
  const text = parts.join(' · ')
  return (
    <span className="min-w-0 truncate font-mono text-[10px] text-ink-3" title={text}>
      {text}
    </span>
  )
}

// ---- Page ----

interface TracePageProps {
  sessionId: string | null
  session?: Session | null
  /** Called when the search box submits a session ID (the parent switches the trace target, triggering a full reload). */
  onSessionIdChange: (id: string) => void
  /** Incremental fetch on change (the turn_finished counter). */
  refreshKey: number
}

/** Top session-ID search: Enter or "View" switches the Trace to that ID. */
function TraceIdSearch({
  currentId,
  onSubmit,
}: {
  currentId: string | null
  onSubmit: (id: string) => void
}) {
  const [value, setValue] = useState('')
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        const id = value.trim()
        if (id) onSubmit(id)
      }}
      className="flex shrink-0 items-center gap-1.5 border-b border-border px-3 py-2"
    >
      <div className="relative min-w-0 max-w-md flex-1">
        <IconSearch className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Enter a session ID to view its trace; Enter to confirm"
          className="w-full rounded-lg border border-border bg-bg py-1.5 pl-7 pr-2 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </div>
      <button
        type="submit"
        className="shrink-0 rounded-lg border border-border bg-bg px-3 py-1.5 text-[12px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
      >
        View
      </button>
      {currentId && (
        <span
          className="ml-1 min-w-0 truncate font-mono text-[11px] text-ink-3"
          title={currentId}
        >
          {currentId}
        </span>
      )}
    </form>
  )
}

/** Trace page: top summary bar + three columns = event timeline | turn/event detail | Inspector. */
export function TracePage({ sessionId, session, onSessionIdChange, refreshKey }: TracePageProps) {
  const [events, setEvents] = useState<RawEnvelope[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [origin, setOrigin] = useState<string>('all')
  const [toggled, setToggled] = useState<Record<number, boolean>>({})
  const [drawerOpen, setDrawerOpen] = useState<Record<number, boolean>>({})
  // Timeline scope: null = main (the root task's events); otherwise limited to one
  // subagent's task_id. Switching to a subagent swaps the turns below to that
  // subagent's own rounds.
  const [scopeTaskId, setScopeTaskId] = useState<string | null>(null)
  // Incremental cursor: the {task_id: last_seq} map echoed by the backend (root +
  // subtask streams count seq independently), sent back verbatim next request.
  const cursorRef = useRef<Record<string, number> | null>(null)
  // The session currently displayed: late-returning in-flight requests of an old
  // session are dropped against this to avoid cross-contamination.
  const sessionRef = useRef(sessionId)

  const load = useCallback(
    async (incremental: boolean) => {
      if (!sessionId) return
      const sid = sessionId
      setLoading(true)
      setError(null)
      try {
        const cursor = incremental ? (cursorRef.current ?? undefined) : undefined
        const r = await adminApi.rawEvents(sid, cursor)
        if (sid !== sessionRef.current) return
        cursorRef.current = r.cursor
        setEvents((cur) => {
          if (!incremental) return r.events
          // Dedup by task_id:seq — seq is only monotonic within its own task
          // stream and collides across streams; also avoids duplicates when an
          // incremental request races a full reload.
          const seen = new Set(cur.map((e) => `${e.task_id}:${e.seq}`))
          const fresh = r.events.filter((e) => !seen.has(`${e.task_id}:${e.seq}`))
          return [...cur, ...fresh]
        })
      } catch (e) {
        if (sid !== sessionRef.current) return
        setError(e instanceof Error ? e.message : 'Failed to load')
      } finally {
        if (sid === sessionRef.current) setLoading(false)
      }
    },
    [sessionId],
  )

  // Session switch: full reload; turn end: incremental top-up.
  useEffect(() => {
    sessionRef.current = sessionId
    setEvents([])
    setSelectedSeq(null)
    setToggled({})
    setDrawerOpen({})
    setScopeTaskId(null)
    cursorRef.current = null
    void load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // Incremental top-up when a turn ends (refreshKey changes); skip the mount frame
  // (the mount already did a full load).
  const mountedRef = useRef(false)
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    void load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  // The execution tree / Inspector / TraceSummary use the full event set (including
  // subtask streams — totals are session-wide); the timeline shows one task stream
  // at a time: root (main) by default, clicking a subagent switches to its stream —
  // each stream counts seq independently, and mixing them would scramble both turn
  // grouping and seq selection.
  const executionTrace = useMemo(() => collectTaskExecution(events), [events])
  const scopedEvents = useMemo(() => {
    const tid = scopeTaskId ?? executionTrace.mainTaskId
    return tid ? events.filter((ev) => ev.task_id === tid) : events
  }, [events, scopeTaskId, executionTrace.mainTaskId])
  const scopedSubagent = scopeTaskId
    ? executionTrace.subagents.find((s) => s.id === scopeTaskId)
    : undefined
  const scopeLabel = scopedSubagent ? subagentLabel(scopedSubagent) : null

  // Grouping is based on the scope's events (groups stay stable); filtering only
  // decides which rows inside a group are visible.
  const groups = useMemo(() => groupByTurn(scopedEvents), [scopedEvents])
  const subagentIds = useMemo(
    () => new Set(executionTrace.subagents.map((subagent) => subagent.id)),
    [executionTrace],
  )
  const subagentLabels = useMemo(() => {
    const map = new Map<string, string>()
    for (const subagent of executionTrace.subagents) {
      map.set(subagent.id, subagentLabel(subagent))
    }
    return map
  }, [executionTrace])
  const hasFilter = query !== '' || origin !== 'all'
  const matchedSeqs = useMemo(() => {
    if (!hasFilter) return null
    const set = new Set<number>()
    for (const ev of scopedEvents) {
      if ((origin === 'all' || ev.origin === origin) && matches(ev, query)) set.add(ev.seq)
    }
    return set
  }, [scopedEvents, query, origin, hasFilter])

  // Event gaps: seq → time delta to the previous event (adjacent within the scope).
  const deltas = useMemo(() => {
    const map = new Map<number, number>()
    for (let i = 1; i < scopedEvents.length; i++) {
      map.set(scopedEvents[i].seq, scopedEvents[i].occurred_at - scopedEvents[i - 1].occurred_at)
    }
    return map
  }, [scopedEvents])

  // Owner badge: in the default full-stream view, mark which subagent an event
  // belongs to; once scoped to a subagent its own events share one owner, so no
  // per-row repetition.
  const ownerFor = useCallback(
    (ev: RawEnvelope): string => {
      const id = eventSubagentId(ev, subagentIds)
      if (!id || id === scopeTaskId) return ''
      return subagentLabels.get(id) ?? ''
    },
    [subagentIds, subagentLabels, scopeTaskId],
  )

  // Scope switch: null = back to main (the root stream); otherwise limit to a
  // subagent. If the current selection is not in the new scope, jump to that
  // scope's first turn so the center column lands on a turn. Collapse state is
  // keyed by group-head seq, which collides across streams — clear it on switch.
  const handleScopeSelect = useCallback(
    (taskId: string | null) => {
      setScopeTaskId(taskId)
      setToggled({})
      setDrawerOpen({})
      const tid = taskId ?? executionTrace.mainTaskId
      setSelectedSeq((cur) => {
        if (
          cur != null &&
          events.some((ev) => ev.seq === cur && (!tid || ev.task_id === tid))
        ) {
          return cur
        }
        return firstTurnSeq(events, tid || null)
      })
    },
    [events, executionTrace.mainTaskId],
  )

  // Inspector compaction-card jump: seq collides across streams, so switch scope by
  // taskId first, then select; also open the turn group containing the target so
  // the row is visible on the timeline (the group id is the group's first seq).
  const handleCompactionJump = useCallback(
    (taskId: string, seq: number) => {
      setScopeTaskId(taskId === executionTrace.mainTaskId ? null : taskId)
      setDrawerOpen({})
      const scoped = events.filter((ev) => ev.task_id === taskId)
      const group = groupByTurn(scoped).find((g) => g.events.some((e) => e.seq === seq))
      setToggled(group ? { [group.id]: true } : {})
      setSelectedSeq(seq)
    },
    [events, executionTrace.mainTaskId],
  )

  const visibleGroups = useMemo(() => {
    if (!matchedSeqs) return groups
    return groups
      .map((g) => ({ group: g, visible: g.events.filter((e) => matchedSeqs.has(e.seq)) }))
      .filter((x) => x.visible.length > 0)
      .map((x) => x.group)
  }, [groups, matchedSeqs])

  const lastGroupId = visibleGroups[visibleGroups.length - 1]?.id
  // Find the selected event inside the scope: seq collides across streams, so a
  // find over the full set could hit another stream.
  const selected = scopedEvents.find((ev) => ev.seq === selectedSeq) ?? null
  const selectedGroup = selected
    ? (groups.find((g) => g.events.some((e) => e.seq === selected.seq)) ?? null)
    : null
  const showTurnView =
    selected != null &&
    selectedGroup?.kind === 'turn' &&
    selectedGroup.round != null &&
    categoryOf(selected.type) === 'llm'

  if (!sessionId) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <TraceIdSearch currentId={null} onSubmit={onSessionIdChange} />
        <div className="flex min-h-0 flex-1 items-center justify-center px-4">
          <p className="max-w-sm text-center text-[13px] leading-relaxed text-ink-3">
            Open a trace from the session list's "View trace", or search by session ID above.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <TraceIdSearch currentId={sessionId} onSubmit={onSessionIdChange} />
      <TraceSummary events={events} />

      <div className="flex min-h-0 flex-1">
        {/* Left column: the event timeline */}
        <div className="flex w-80 shrink-0 flex-col border-r border-border">
          <div className="shrink-0 space-y-2 border-b border-border p-2.5">
            <div className="flex items-center gap-1.5">
              <div className="relative min-w-0 flex-1">
                <IconSearch className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search event types / payloads…"
                  className="w-full rounded-lg border border-border bg-bg py-1.5 pl-7 pr-2 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
                />
              </div>
              <button
                type="button"
                onClick={() => void load(true)}
                disabled={loading}
                title="Fetch new events"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink disabled:opacity-50"
              >
                <IconRefresh className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
              </button>
            </div>
            <select
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
              className="w-full cursor-pointer rounded-lg border border-border bg-bg px-2 py-1 font-mono text-[11px] text-ink-2 outline-none focus:border-accent"
            >
              {ORIGINS.map((o) => (
                <option key={o} value={o}>
                  {`origin: ${o}`}
                </option>
              ))}
            </select>
            <ExecutionTree
              trace={executionTrace}
              activeTaskId={scopeTaskId}
              onSelect={handleScopeSelect}
            />
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
            {error && (
              <p className="m-1.5 rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[12px] text-danger">
                {error}
              </p>
            )}
            {!error && visibleGroups.length === 0 && (
              <p className="p-4 text-center text-[12.5px] text-ink-3">
                {loading ? 'Loading…' : events.length > 0 ? 'No matching events.' : 'No events yet.'}
              </p>
            )}
            {visibleGroups.map((g) => {
              const open = toggled[g.id] ?? g.id === lastGroupId
              const visible = matchedSeqs
                ? g.events.filter((e) => matchedSeqs.has(e.seq))
                : g.events
              // While filtering, skip the drawer folding (events the user explicitly
              // searched for show directly).
              const drawer = matchedSeqs ? [] : visible.filter((e) => isDrawerType(e.type))
              const main = matchedSeqs ? visible : visible.filter((e) => !isDrawerType(e.type))
              const dOpen = drawerOpen[g.id] ?? false
              return (
                <div key={g.id} className="mb-0.5">
                  <button
                    type="button"
                    onClick={() => setToggled((t) => ({ ...t, [g.id]: !open }))}
                    className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left hover:bg-surface-2"
                  >
                    <IconChevron open={open} className="h-3 w-3 shrink-0 text-ink-3" />
                    <span className="shrink-0 text-[12px] font-medium text-ink">{g.label}</span>
                    <TurnHeaderInfo group={g} />
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-ink-3">
                      {visible.length}
                    </span>
                  </button>
                  {open && (
                    <ul>
                      {drawer.length > 0 && (
                        <li>
                          <button
                            type="button"
                            onClick={() =>
                              setDrawerOpen((d) => ({ ...d, [g.id]: !dOpen }))
                            }
                            className="flex w-full items-center gap-1.5 rounded-md py-1 pl-6 pr-2 text-left hover:bg-surface-2"
                          >
                            <IconChevron open={dOpen} className="h-2.5 w-2.5 shrink-0 text-ink-3" />
                            <span className="font-mono text-[10.5px] text-ink-3">
                              raw events · {drawer.length}
                            </span>
                          </button>
                          {dOpen &&
                            drawer.map((ev) => (
                              <EventRow
                                key={ev.seq}
                                ev={ev}
                                selected={selectedSeq === ev.seq}
                                delta={deltas.get(ev.seq) ?? null}
                                owner={ownerFor(ev)}
                                dimmed
                                onSelect={setSelectedSeq}
                              />
                            ))}
                        </li>
                      )}
                      {main.map((ev) => (
                        <li key={ev.seq}>
                          <EventRow
                            ev={ev}
                            selected={selectedSeq === ev.seq}
                            delta={deltas.get(ev.seq) ?? null}
                            owner={ownerFor(ev)}
                            onSelect={setSelectedSeq}
                          />
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* Center column: turn / event detail */}
        <div className="min-w-0 flex-1 overflow-hidden">
          {selected == null ? (
            <p className="p-6 text-center text-[12.5px] text-ink-3">
              Select an event in the timeline to see its detail.
            </p>
          ) : showTurnView ? (
            <TurnView
              // Remount the whole tree per turn: internal state such as MessageCard
              // collapse must not leak across turns (the turn group's id is
              // LLMRequestStarted.seq — stable and always present).
              key={selectedGroup!.id}
              round={selectedGroup!.round!}
              turnEvents={selectedGroup!.events}
              turnLabel={
                scopeLabel ? `${scopeLabel} · ${selectedGroup!.label}` : selectedGroup!.label
              }
              selected={selected}
            />
          ) : (
            <EventDetail event={selected} />
          )}
        </div>

        {/* Right column: Inspector */}
        <div className="flex w-72 shrink-0 flex-col border-l border-border">
          <div className="shrink-0 border-b border-border px-3 py-2">
            <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">
              Inspector
            </span>
          </div>
          <Inspector events={events} session={session ?? null} onJump={handleCompactionJump} />
        </div>
      </div>
    </div>
  )
}
