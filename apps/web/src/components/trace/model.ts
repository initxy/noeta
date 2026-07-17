/** Pure data-folding layer of the Trace page: ContentRef detection, event
 *  classification, turn grouping, aggregate stats. Everything folds out of the raw
 *  envelope stream with no component state. */
import type { RawEnvelope } from '../../api/types'

// ---- ContentRef ----

/** ContentRef as serialized by noeta to_canonical (kept verbatim inside payloads). */
export interface ContentRefJson {
  __canonical_tag__: 'content_ref'
  hash: string
  size: number
  media_type: string
}

export function isContentRef(v: unknown): v is ContentRefJson {
  return (
    typeof v === 'object' &&
    v !== null &&
    (v as Record<string, unknown>).__canonical_tag__ === 'content_ref' &&
    typeof (v as Record<string, unknown>).hash === 'string'
  )
}

// ---- Event classification ----

export type EventCategory =
  | 'lifecycle'
  | 'tool'
  | 'llm'
  | 'context'
  | 'governance'
  | 'message'

const CATEGORY_BY_TYPE: Record<string, EventCategory> = {
  LLMRequestStarted: 'llm',
  LLMResponseRecorded: 'llm',
  LLMRequestFinished: 'llm',
  AssistantThinkingRecorded: 'llm',
  ToolCallStarted: 'tool',
  ToolResultRecorded: 'tool',
  ToolCallFinished: 'tool',
  ToolCallDenied: 'governance',
  ToolCallApprovalRequested: 'governance',
  ContextPlanComposed: 'context',
  ContextContentRecorded: 'context',
  CompactionRequested: 'context',
  Compacted: 'context',
  MessagesAppended: 'message',
  UserQuestionRequested: 'message',
  UserQuestionAnswered: 'message',
}

/** Unlisted types (TaskCreated/TaskStarted/TaskSuspended…) all count as lifecycle. */
export function categoryOf(type: string): EventCategory {
  return CATEGORY_BY_TYPE[type] ?? 'lifecycle'
}

export const CATEGORY_COLORS: Record<EventCategory, string> = {
  lifecycle: 'text-ink-3',
  llm: 'text-accent',
  tool: 'text-warn',
  context: 'text-ink-2',
  governance: 'text-danger',
  message: 'text-ink',
}

/** Lifecycle plumbing events folded into the "raw events" drawer by default (kept
 *  off the main timeline). Terminal states (TaskCompleted/Failed/Cancelled) and
 *  Subtask events stay visible. */
const DRAWER_TYPES = new Set([
  'TaskCreated',
  'TaskHostBound',
  'AgentBound',
  'ModelBound',
  'TaskStarted',
  'TaskSuspended',
  'TaskWoken',
  'TaskSnapshot',
  'TaskRewound',
  'TaskStatePatched',
  'StepTransitionMarked',
])

export function isDrawerType(type: string): boolean {
  return DRAWER_TYPES.has(type)
}

// ---- Payload summaries (the one-liner on a timeline row) ----

type P = Record<string, unknown>

function payloadOf(ev: RawEnvelope): P {
  return typeof ev.payload === 'object' && ev.payload !== null
    ? (ev.payload as P)
    : {}
}

export interface UsageJson {
  uncached: number
  cache_read: number
  cache_write: number
  output: number
  reasoning_tokens: number
}

export function usageInput(u: UsageJson | undefined): number {
  if (!u) return 0
  return (u.uncached ?? 0) + (u.cache_read ?? 0) + (u.cache_write ?? 0)
}

export function summaryOf(ev: RawEnvelope): string {
  const p = payloadOf(ev)
  const s = (v: unknown) => (typeof v === 'string' ? v : '')
  switch (ev.type) {
    case 'TaskCreated':
      return s(p.goal)
    case 'LLMRequestStarted':
      return s(p.model)
    case 'LLMResponseRecorded':
      return s(p.stop_reason)
    case 'LLMRequestFinished': {
      const usage = p.usage as UsageJson | undefined
      const base = `${usageInput(usage)}→${usage?.output ?? 0} tok · ${p.latency_ms ?? 0}ms`
      return p.success === false ? `failed · ${base}` : base
    }
    case 'ToolCallStarted':
      return s(p.tool_name)
    case 'ToolResultRecorded':
      return `${p.success === false ? 'failed · ' : ''}${s(p.summary)}`
    case 'MessagesAppended':
      return `+${p.count ?? 0} messages`
    case 'UserQuestionRequested':
      return s(p.reason)
    case 'UserQuestionAnswered':
      return s(p.question_id)
    case 'TaskSuspended':
      return s(p.reason)
    case 'TaskCompleted':
      return typeof p.answer === 'string' ? p.answer : ''
    case 'TaskFailed':
      return s(p.reason)
    case 'ModelBound':
      return s(p.model)
    case 'SubtaskSpawned':
      return `${s(p.agent_name)}: ${s(p.goal)}`
    case 'BackgroundSubagentStarted':
      return `${s(p.agent_name)}: ${s(p.goal)}`
    case 'BackgroundSubagentDelivered':
      return `${s(p.status)} · ${s(p.summary)}`
    case 'SubtaskCompleted':
      return s(p.subtask_id)
    case 'CompactionRequested':
      // The label already carries the reason (proactive/passive); don't append it twice.
      return compactionLabel(compactionKindOf(ev))
    case 'Compacted':
      // Compacted carries no reason, so kind is always unknown → bare
      // 'summary compaction'; the trigger reason lives on the preceding
      // CompactionRequested.
      return [
        compactionLabel(compactionKindOf(ev)),
        typeof p.replaced_count === 'number' ? `folded ${p.replaced_count} messages` : '',
      ]
        .filter(Boolean)
        .join(' · ')
    case 'ContextContentRecorded':
      return `${s(p.kind)}/${s(p.name)} v${s(p.version)}`
    default:
      return ''
  }
}

// ---- subagent / context-compaction folding ----

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function getPayload(ev: RawEnvelope): P {
  return payloadOf(ev)
}

export type SubagentStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'unknown'

export interface SubagentTrace {
  id: string
  index: number
  agentName: string
  goal: string
  status: SubagentStatus
  startSeq: number | null
  endSeq: number | null
  eventCount: number
  summary: string
}

export interface TaskExecutionTrace {
  mainTaskId: string
  mainStatus: string
  mainEventCount: number
  subagents: SubagentTrace[]
}

function statusFromResult(v: unknown): SubagentStatus {
  if (typeof v === 'object' && v !== null) return statusFromResult((v as P).status)
  const status = str(v)
  if (status === 'completed' || status === 'failed' || status === 'cancelled') return status
  return status ? 'unknown' : 'completed'
}

export function subagentIdOf(ev: RawEnvelope): string {
  const p = getPayload(ev)
  return str(p.subtask_id) || str(p.subagent_id)
}

export function eventSubagentId(ev: RawEnvelope, knownIds: Set<string>): string {
  const direct = subagentIdOf(ev)
  if (direct) return direct
  return knownIds.has(ev.task_id) ? ev.task_id : ''
}

export function subagentLabel(subagent: SubagentTrace): string {
  return `#${subagent.index} ${subagent.agentName || 'subagent'}`
}

export function collectTaskExecution(events: RawEnvelope[]): TaskExecutionTrace {
  const mainTaskId = events[0]?.task_id ?? ''
  let mainStatus = 'unknown'
  const byId = new Map<string, SubagentTrace>()
  const ensure = (id: string): SubagentTrace => {
    const existing = byId.get(id)
    if (existing) return existing
    const next: SubagentTrace = {
      id,
      index: byId.size + 1,
      agentName: '',
      goal: '',
      status: 'unknown',
      startSeq: null,
      endSeq: null,
      eventCount: 0,
      summary: '',
    }
    byId.set(id, next)
    return next
  }

  for (const ev of events) {
    const p = getPayload(ev)
    if (ev.type === 'TaskStarted' || ev.type === 'TaskWoken') mainStatus = 'running'
    else if (ev.type === 'TaskSuspended') mainStatus = str(p.reason) || 'suspended'
    else if (ev.type === 'TaskCompleted') mainStatus = 'completed'
    else if (ev.type === 'TaskFailed') mainStatus = 'failed'
    else if (ev.type === 'TaskCancelled') mainStatus = 'cancelled'

    if (ev.type === 'BackgroundSubagentStarted' || ev.type === 'SubtaskSpawned') {
      const id = subagentIdOf(ev)
      if (id) {
        const sub = ensure(id)
        sub.agentName = str(p.agent_name) || sub.agentName
        sub.goal = str(p.goal) || sub.goal
        sub.status = 'running'
        sub.startSeq = sub.startSeq ?? ev.seq
      }
    } else if (ev.type === 'BackgroundSubagentDelivered' || ev.type === 'SubtaskCompleted') {
      const id = subagentIdOf(ev)
      if (id) {
        const sub = ensure(id)
        sub.status = ev.type === 'SubtaskCompleted' ? statusFromResult(p.result) : statusFromResult(p.status)
        sub.endSeq = ev.seq
        sub.summary = str(p.summary) || str((p.result as P | undefined)?.error) || sub.summary
      }
    }
  }

  const knownIds = new Set(byId.keys())
  for (const ev of events) {
    const id = eventSubagentId(ev, knownIds)
    if (!id) continue
    ensure(id).eventCount += 1
  }

  return {
    mainTaskId,
    mainStatus,
    mainEventCount: events.filter((ev) => !knownIds.has(ev.task_id)).length,
    subagents: Array.from(byId.values()),
  }
}

/** Trigger reason of a macro compaction (summarize), read from noeta
 *  `CompactionRequested.reason`: `proactive` = the threshold estimator fired and
 *  compacted before the request went out; `passive` = the provider already reported
 *  an overflow and this is the fallback. Any other value (added by newer SDKs)
 *  passes through verbatim — no guessing. */
export type CompactionKind = 'proactive' | 'passive' | 'unknown'

/** One macro compaction = a CompactionRequested paired with the Compacted that
 *  follows it (request first, then landing, within the same task stream). The
 *  Inspector's compaction section shows "one card per compaction", not one per event. */
export interface CompactionTrace {
  /** Owning task stream (subagents may compact too; seq collides across streams, so
   *  jumps must switch scope first). */
  taskId: string
  /** Seq of the CompactionRequested; an orphan Compacted (no preceding request) uses its own seq. */
  seq: number
  /** Seq of the Compacted; null = request seen but no landing (e.g. interrupted by anti-spiral escalation). */
  compactedSeq: number | null
  occurredAt: number
  kind: CompactionKind
  label: string
  /** chars/4 estimate. noeta only attaches it on CompactionRequested, and it
   *  **systematically underestimates** CJK/code payloads (~4x in practice) — the
   *  real trigger works off blended real usage, not this number. Read it as "the
   *  estimator's reading", not "how big the context was". */
  estimatedTokens: number | null
  /** Number of messages folded away (Compacted.replaced_count). */
  replacedCount: number | null
}

export function compactionKindOf(ev: RawEnvelope): CompactionKind {
  const reason = str(getPayload(ev).reason).toLowerCase()
  if (reason === 'proactive' || reason === 'passive') return reason
  return 'unknown'
}

export function compactionLabel(kind: CompactionKind): string {
  if (kind === 'proactive') return 'summary compaction · proactive'
  if (kind === 'passive') return 'summary compaction · overflow fallback'
  return 'summary compaction'
}

export function collectCompactions(events: RawEnvelope[]): CompactionTrace[] {
  const out: CompactionTrace[] = []
  // Only one in-flight compaction per task stream: a Requested hangs pending until
  // a Compacted claims it.
  const pending = new Map<string, CompactionTrace>()
  for (const ev of events) {
    const p = getPayload(ev)
    if (ev.type === 'CompactionRequested') {
      const stale = pending.get(ev.task_id)
      if (stale) out.push(stale)
      const kind = compactionKindOf(ev)
      pending.set(ev.task_id, {
        taskId: ev.task_id,
        seq: ev.seq,
        compactedSeq: null,
        occurredAt: ev.occurred_at,
        kind,
        label: compactionLabel(kind),
        estimatedTokens: num(p.estimated_tokens),
        replacedCount: null,
      })
    } else if (ev.type === 'Compacted') {
      const req = pending.get(ev.task_id)
      if (req) {
        pending.delete(ev.task_id)
        out.push({
          ...req,
          compactedSeq: ev.seq,
          replacedCount: num(p.replaced_count),
        })
      } else {
        out.push({
          taskId: ev.task_id,
          seq: ev.seq,
          compactedSeq: ev.seq,
          occurredAt: ev.occurred_at,
          kind: 'unknown',
          label: compactionLabel('unknown'),
          estimatedTokens: null,
          replacedCount: num(p.replaced_count),
        })
      }
    }
  }
  out.push(...pending.values())
  return out.sort((a, b) => a.occurredAt - b.occurredAt)
}

// ---- LLM round pairing and grouping ----

/** One LLM round-trip: LLMRequestStarted / Finished / ResponseRecorded paired by call_id. */
export interface LlmRound {
  callId: string
  started: RawEnvelope
  finished?: RawEnvelope
  response?: RawEnvelope
}

export interface TurnGroup {
  /** Seq of the group's first event, used as the group id. */
  id: number
  label: string
  /** turn = a round opened by LLMRequestStarted; init = events before the first round; legacy = old-session fallback grouping. */
  kind: 'turn' | 'init' | 'legacy'
  events: RawEnvelope[]
  round?: LlmRound
}

/** Pair the LLM round trio by call_id within a slice of events. */
export function pairRound(events: RawEnvelope[], callId: string): LlmRound | null {
  let started: RawEnvelope | undefined
  let finished: RawEnvelope | undefined
  let response: RawEnvelope | undefined
  for (const ev of events) {
    const p = payloadOf(ev)
    if (p.call_id !== callId) continue
    if (ev.type === 'LLMRequestStarted') started = ev
    else if (ev.type === 'LLMRequestFinished') finished = ev
    else if (ev.type === 'LLMResponseRecorded') response = ev
  }
  if (!started) return null
  return { callId, started, finished, response }
}

/** Group by LLMRequestStarted; events before the first round go to "Init".
 *  Old sessions without LLMRequestStarted fall back to grouping by TaskStarted/TaskWoken. */
export function groupByTurn(events: RawEnvelope[]): TurnGroup[] {
  const hasLlm = events.some((ev) => ev.type === 'LLMRequestStarted')
  if (!hasLlm) return groupLegacy(events)

  const groups: TurnGroup[] = []
  let turn = 0
  for (const ev of events) {
    if (ev.type === 'LLMRequestStarted') {
      turn += 1
      groups.push({
        id: ev.seq,
        label: `Turn ${turn}`,
        kind: 'turn',
        events: [ev],
      })
      continue
    }
    const cur = groups[groups.length - 1]
    if (cur) cur.events.push(ev)
    else groups.push({ id: ev.seq, label: 'Init', kind: 'init', events: [ev] })
  }
  for (const g of groups) {
    if (g.kind !== 'turn') continue
    const callId = (payloadOf(g.events[0]).call_id as string) ?? ''
    g.round = pairRound(g.events, callId) ?? undefined
  }
  return groups
}

function groupLegacy(events: RawEnvelope[]): TurnGroup[] {
  const groups: TurnGroup[] = []
  let turn = 0
  for (const ev of events) {
    if (ev.type === 'TaskStarted' || ev.type === 'TaskWoken') {
      turn += 1
      groups.push({ id: ev.seq, label: `Round ${turn}`, kind: 'legacy', events: [ev] })
      continue
    }
    const cur = groups[groups.length - 1]
    if (cur) cur.events.push(ev)
    else groups.push({ id: ev.seq, label: 'Init', kind: 'init', events: [ev] })
  }
  return groups
}

/** Turn-group header stats: model · in→out tok · $cost · duration. */
export interface TurnStats {
  model: string
  tokensIn: number
  tokensOut: number
  costUsd: number
  durationS: number
}

export function turnStats(g: TurnGroup): TurnStats | null {
  if (g.kind !== 'turn' || !g.round) return null
  const started = payloadOf(g.round.started)
  const finished = g.round.finished ? payloadOf(g.round.finished) : undefined
  const usage = finished?.usage as UsageJson | undefined
  const last = g.events[g.events.length - 1]
  return {
    model: (started.model as string) ?? '',
    tokensIn: usageInput(usage),
    tokensOut: usage?.output ?? 0,
    costUsd: (finished?.cost_usd as number) ?? 0,
    durationS: last.occurred_at - g.round.started.occurred_at,
  }
}

// ---- Session-level totals (the top TraceSummary bar) ----

export interface TraceTotals {
  model: string
  events: number
  rounds: number
  subagents: number
  compactions: number
  summaryCompactions: number
  tokensIn: number
  tokensOut: number
  cacheRead: number
  costUsd: number
  durationS: number
}

export function traceTotals(events: RawEnvelope[]): TraceTotals {
  let model = ''
  let rounds = 0
  const subagents = new Set<string>()
  let compactions = 0
  let summaryCompactions = 0
  let tokensIn = 0
  let tokensOut = 0
  let cacheRead = 0
  let costUsd = 0
  for (const ev of events) {
    const p = payloadOf(ev)
    if (ev.type === 'LLMRequestStarted') {
      rounds += 1
      if (typeof p.model === 'string') model = p.model
    } else if (ev.type === 'LLMRequestFinished') {
      const usage = p.usage as UsageJson | undefined
      tokensIn += usageInput(usage)
      tokensOut += usage?.output ?? 0
      cacheRead += usage?.cache_read ?? 0
      costUsd += (p.cost_usd as number) ?? 0
    } else if (ev.type === 'ModelBound' && !model && typeof p.model === 'string') {
      model = p.model
    }
    if (ev.type === 'BackgroundSubagentStarted' || ev.type === 'SubtaskSpawned') {
      const id = subagentIdOf(ev)
      if (id) subagents.add(id)
    }
    // Only macro compactions (summarize) emit events. Micro compactions (the
    // composer's prune) emit nothing — their only trace is
    // ContextPlan.cleared_outputs, which requires dereferencing plan_ref and is
    // out of reach for this synchronous reduce; the per-turn context provenance
    // section covers the display.
    if (ev.type === 'Compacted') summaryCompactions += 1
    if (ev.type === 'CompactionRequested' || ev.type === 'Compacted') {
      compactions += 1
    }
  }
  const durationS =
    events.length > 1 ? events[events.length - 1].occurred_at - events[0].occurred_at : 0
  return {
    model,
    events: events.length,
    rounds,
    subagents: subagents.size,
    compactions,
    summaryCompactions,
    tokensIn,
    tokensOut,
    cacheRead,
    costUsd,
    durationS,
  }
}

// ---- Display formatting ----

export function clock(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString('en-GB', { hour12: false })
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

export function fmtSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${bytes}B`
}

export function fmtDuration(s: number): string {
  if (s >= 60) return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
  return `${s.toFixed(1)}s`
}
