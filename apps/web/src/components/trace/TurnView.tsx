/** Turn detail shown when an LLM event is selected: header badges, system prompt,
 *  tools, conversation (one card per role), output, this turn's tool-call pairs,
 *  context provenance, collapsible raw payload. Data comes from the call_id-paired
 *  trio + derefs of request_ref/response_ref. */
import { useMemo, useState, type ReactNode } from 'react'
import type { RawEnvelope } from '../../api/types'
import { cn } from '../../lib/cn'
import { IconChevron } from '../icons'
import {
  fmtDuration,
  fmtTokens,
  isContentRef,
  usageInput,
  type ContentRefJson,
  type LlmRound,
  type UsageJson,
} from './model'
import { ClampText, JsonTree, RefChip } from './RefChip'
import { useContentBody } from './useContentBody'

type P = Record<string, unknown>

function payloadOf(ev: RawEnvelope | undefined): P {
  return ev && typeof ev.payload === 'object' && ev.payload !== null
    ? (ev.payload as P)
    : {}
}

// ---- Compaction evidence (all hidden inside the ContextPlan; only reachable by dereferencing plan_ref) ----

/** Per-segment hashes of the three-part layout (hot/cold layering). stable_prefix
 *  never changing = the prefix cache can hit; dynamic_suffix changing every round is
 *  normal. Which segment changed directly determines the cache-invalidation scope. */
interface PlanJson {
  cleared_outputs?: unknown[]
  segment_hashes?: Record<string, string>
}

/** Micro compaction (the composer's prune) and hot/cold layering emit no events;
 *  their only trace is the ContextPlan's cleared_outputs / segment_hashes. Without
 *  dereferencing they are entirely invisible — a fix could never be verified — so
 *  spread them out here instead of making people click the RefChip and read fields
 *  themselves. */
function PlanCompaction({ refJson }: { refJson: ContentRefJson }) {
  const { body } = useContentBody(refJson.hash)
  const plan = useMemo<PlanJson | null>(() => {
    if (typeof body !== 'string') return null
    try {
      return JSON.parse(body) as PlanJson
    } catch {
      return null
    }
  }, [body])
  if (!plan) return null
  const cleared = Array.isArray(plan.cleared_outputs) ? plan.cleared_outputs.length : 0
  const segs = plan.segment_hashes ?? {}
  const segLabel = ['stable_prefix', 'semi_stable', 'dynamic_suffix']
    .filter((k) => k in segs)
    .map((k) => `${k.replace('_prefix', '').replace('_suffix', '')} ${segs[k].slice(0, 6)}`)
    .join(' · ')
  return (
    <div className="flex flex-wrap gap-1.5">
      <Badge
        label="micro compaction"
        value={cleared > 0 ? `cleared ${cleared} tool output${cleared === 1 ? '' : 's'}` : 'not triggered'}
        tone={cleared > 0 ? 'accent' : undefined}
      />
      {segLabel && <Badge label="segments" value={segLabel} />}
    </div>
  )
}

// ---- Shared bits ----

function Badge({ label, value, tone }: { label?: string; value: string; tone?: 'accent' | 'danger' }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10.5px]',
        tone === 'danger'
          ? 'border-danger/30 bg-danger-soft text-danger'
          : tone === 'accent'
            ? 'border-accent/30 bg-accent-soft text-accent'
            : 'border-border bg-surface-2 text-ink-2',
      )}
    >
      {label && <span className="text-ink-3">{label}</span>}
      {value}
    </span>
  )
}

function Section({
  title,
  badge,
  defaultOpen = true,
  children,
}: {
  title: string
  badge?: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border-b border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-4 py-2 text-left hover:bg-surface-2"
      >
        <IconChevron open={open} className="h-3 w-3 shrink-0 text-ink-3" />
        <span className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
          {title}
        </span>
        {badge && <span className="font-mono text-[10.5px] text-ink-3">{badge}</span>}
      </button>
      {open && <div className="space-y-2 px-4 pb-3">{children}</div>}
    </div>
  )
}

// ---- Content blocks (shared by request messages and response output) ----

function BlockView({ block }: { block: unknown }) {
  if (typeof block !== 'object' || block === null) return <JsonTree value={block} />
  const b = block as P
  const tag = b.__canonical_tag__
  if (tag === 'text_block') {
    return <ClampText text={String(b.text ?? '')} />
  }
  if (tag === 'thinking_block') {
    return (
      <div className="border-l-2 border-border-strong pl-2 italic text-ink-3">
        <ClampText text={String(b.text ?? '')} />
      </div>
    )
  }
  if (tag === 'tool_use_block') {
    return (
      <div className="rounded-md border border-border bg-bg p-2">
        <p className="mb-1 font-mono text-[11px] text-warn">
          tool_use · {String(b.tool_name ?? '')}
          <span className="ml-2 text-ink-3">{String(b.call_id ?? '')}</span>
        </p>
        <JsonTree value={b.arguments} />
      </div>
    )
  }
  if (tag === 'tool_result_block') {
    return (
      <div className="rounded-md border border-border bg-bg p-2">
        <p className="mb-1 font-mono text-[11px] text-warn">
          tool_result
          <span className="ml-2 text-ink-3">{String(b.call_id ?? '')}</span>
          {b.success === false && <span className="ml-2 text-danger">failed</span>}
        </p>
        {isContentRef(b.output) ? (
          <RefChip refJson={b.output} label="output" />
        ) : typeof b.output === 'string' ? (
          <ClampText text={b.output} />
        ) : (
          <JsonTree value={b.output} />
        )}
        {typeof b.error === 'string' && b.error && (
          <p className="mt-1 font-mono text-[11px] text-danger">{b.error}</p>
        )}
      </div>
    )
  }
  if (tag === 'image_block' && isContentRef(b.source)) {
    return <RefChip refJson={b.source} label="image" />
  }
  return <JsonTree value={block} />
}

const ROLE_STYLES: Record<string, string> = {
  user: 'border-accent/30',
  assistant: 'border-border-strong',
  tool: 'border-warn/30',
  system: 'border-border',
}

// ---- Message folding (mirrors noeta trace's MessageCard: collapsed by default to
//      one line — chevron + role + preview + rough token estimate; click to expand
//      the full blocks) ----

/** Plain text of all blocks of a message (input for the preview and the rough token estimate), mirroring noeta messageText. */
function messageText(msg: P): string {
  const blocks = Array.isArray(msg.content) ? msg.content : []
  return blocks
    .map((b) => {
      if (typeof b !== 'object' || b === null) return ''
      const o = b as P
      const tag = o.__canonical_tag__
      if (tag === 'text_block' || tag === 'thinking_block') return String(o.text ?? '')
      if (tag === 'tool_use_block')
        return `→ ${String(o.tool_name ?? 'tool')} ${JSON.stringify(o.arguments ?? {})}`
      if (tag === 'tool_result_block')
        return typeof o.output === 'string' ? o.output : JSON.stringify(o.output ?? '')
      return JSON.stringify(o)
    })
    .join('\n')
}

function previewLine(text: string): string {
  const s = text.replace(/\s+/g, ' ').trim()
  if (!s) return '(empty)'
  return s.length > 96 ? `${s.slice(0, 96)}…` : s
}

/** Rough ~4 chars/token estimate (mirroring noeta approxTokens): only labels messages without a real count. */
function approxTokens(text: string): number {
  return text ? Math.max(1, Math.ceil(text.length / 4)) : 0
}

function MessageCard({ msg, defaultOpen = false }: { msg: P; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const role = String(msg.role ?? '?')
  const blocks = Array.isArray(msg.content) ? msg.content : []
  const text = useMemo(() => messageText(msg), [msg])
  return (
    <div
      className={cn(
        'overflow-hidden rounded-lg border bg-surface',
        ROLE_STYLES[role] ?? 'border-border',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left hover:bg-surface-2"
      >
        <IconChevron open={open} className="h-2.5 w-2.5 shrink-0 text-ink-3" />
        <span className="shrink-0 font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-3">
          {role}
          {typeof msg.origin === 'string' && (
            <span className="normal-case tracking-normal"> · {msg.origin}</span>
          )}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-2">
          {previewLine(text)}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-ink-3">
          ~{fmtTokens(approxTokens(text))} tok
        </span>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border p-2.5 text-[12px]">
          {blocks.length > 0 ? (
            blocks.map((b, i) => <BlockView key={i} block={b} />)
          ) : (
            <p className="text-[11.5px] text-ink-3">(no content)</p>
          )}
        </div>
      )}
    </div>
  )
}

// ---- Request / response bodies ----

function useJsonBody(ref: ContentRefJson | null): { data: unknown; loading: boolean; error: string | null } {
  const { body, loading, error } = useContentBody(ref?.hash ?? null)
  const data = useMemo(() => {
    if (body == null) return undefined
    try {
      return JSON.parse(body) as unknown
    } catch {
      return undefined
    }
  }, [body])
  return { data, loading, error }
}

function LoadState({ loading, error }: { loading: boolean; error: string | null }) {
  if (loading) return <p className="font-mono text-[11px] text-ink-3">Loading…</p>
  if (error) return <p className="font-mono text-[11px] text-danger">{error}</p>
  return null
}

function textOfMessage(msg: unknown): string {
  if (typeof msg !== 'object' || msg === null) return ''
  const blocks = (msg as P).content
  if (!Array.isArray(blocks)) return ''
  return blocks
    .map((b) =>
      typeof b === 'object' && b !== null && (b as P).__canonical_tag__ === 'text_block'
        ? String((b as P).text ?? '')
        : '',
    )
    .join('\n')
    .trim()
}

function toolName(t: unknown): string {
  if (typeof t !== 'object' || t === null) return '?'
  const o = t as P
  const fn = o.function
  if (typeof fn === 'object' && fn !== null && typeof (fn as P).name === 'string') {
    return (fn as P).name as string
  }
  return typeof o.name === 'string' ? o.name : '?'
}

function ToolSpecRow({ spec }: { spec: unknown }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-md border border-border bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left hover:bg-surface-2"
      >
        <IconChevron open={open} className="h-2.5 w-2.5 shrink-0 text-ink-3" />
        <span className="font-mono text-[11px] text-ink">{toolName(spec)}</span>
      </button>
      {open && (
        <div className="border-t border-border p-2">
          <JsonTree value={spec} />
        </div>
      )}
    </div>
  )
}

// ---- This turn's tool-call pairs ----

interface ToolPair {
  callId: string
  started?: RawEnvelope
  result?: RawEnvelope
  denied?: RawEnvelope
}

function pairTools(turnEvents: RawEnvelope[]): ToolPair[] {
  const byId = new Map<string, ToolPair>()
  const order: string[] = []
  for (const ev of turnEvents) {
    if (!['ToolCallStarted', 'ToolResultRecorded', 'ToolCallDenied'].includes(ev.type)) continue
    const callId = String(payloadOf(ev).call_id ?? '')
    let pair = byId.get(callId)
    if (!pair) {
      pair = { callId }
      byId.set(callId, pair)
      order.push(callId)
    }
    if (ev.type === 'ToolCallStarted') pair.started = ev
    else if (ev.type === 'ToolResultRecorded') pair.result = ev
    else pair.denied = ev
  }
  return order.map((id) => byId.get(id)!)
}

/** The memory capability runs through the ordinary tool channel (write/read/search/archive); label the operation by tool_name. */
function memoryOpLabel(toolName: string): string | null {
  if (toolName === 'memory_write') return 'memory · write'
  if (toolName === 'memory_read') return 'memory · read'
  if (toolName === 'memory_search') return 'memory · search'
  if (toolName === 'memory_archive') return 'memory · archive'
  return null
}

function ToolPairCard({ pair }: { pair: ToolPair }) {
  const started = payloadOf(pair.started)
  const result = payloadOf(pair.result)
  const denied = payloadOf(pair.denied)
  const artifacts = Array.isArray(result.artifacts) ? result.artifacts : []
  const toolName = String(started.tool_name ?? denied.tool_name ?? '?')
  const memoryOp = memoryOpLabel(toolName)
  return (
    <div className="rounded-lg border border-border bg-surface p-2.5">
      <p className="mb-1.5 font-mono text-[11.5px] text-ink">
        <span className="text-warn">{toolName}</span>
        {memoryOp && (
          <span className="ml-2 rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
            {memoryOp}
          </span>
        )}
        <span className="ml-2 text-[10.5px] text-ink-3">{pair.callId}</span>
        {pair.denied && <span className="ml-2 text-[10.5px] text-danger">denied</span>}
        {result.success === false && <span className="ml-2 text-[10.5px] text-danger">failed</span>}
      </p>
      {pair.started && (
        <div className="mb-1.5">
          {isContentRef(started.arguments_ref) ? (
            <RefChip refJson={started.arguments_ref} label="arguments" />
          ) : (
            started.arguments != null && <JsonTree value={started.arguments} />
          )}
        </div>
      )}
      {typeof denied.reason === 'string' && denied.reason && (
        <p className="mb-1.5 font-mono text-[11px] text-danger">{denied.reason}</p>
      )}
      {pair.result && (
        <div className="space-y-1">
          {typeof result.summary === 'string' && result.summary && (
            <p className="text-[11.5px] text-ink-2">{result.summary}</p>
          )}
          {isContentRef(result.output_ref) && (
            <RefChip refJson={result.output_ref} label="output" />
          )}
          {artifacts.filter(isContentRef).map((a) => (
            <RefChip key={a.hash} refJson={a} label="artifact" />
          ))}
        </div>
      )}
    </div>
  )
}

// ---- Main component ----

interface TurnViewProps {
  round: LlmRound
  /** All events of this (grouped) turn, for tool pairing and context provenance. */
  turnEvents: RawEnvelope[]
  turnLabel: string
  /** The currently selected event (shown in the raw payload section). */
  selected: RawEnvelope
}

export function TurnView({ round, turnEvents, turnLabel, selected }: TurnViewProps) {
  const started = payloadOf(round.started)
  const finished = payloadOf(round.finished)
  const response = payloadOf(round.response)
  const usage = finished.usage as UsageJson | undefined

  const requestRef = isContentRef(started.request_ref) ? started.request_ref : null
  const responseRef = isContentRef(response.response_ref) ? response.response_ref : null
  const req = useJsonBody(requestRef)
  const resp = useJsonBody(responseRef)

  const reqData = (typeof req.data === 'object' && req.data !== null ? req.data : {}) as P
  const respData = (typeof resp.data === 'object' && resp.data !== null ? resp.data : {}) as P
  const messages = Array.isArray(reqData.messages) ? reqData.messages : []
  const tools = Array.isArray(reqData.tools) ? reqData.tools : []
  const systemText = textOfMessage(reqData.system)
  const outputBlocks = Array.isArray(respData.content) ? respData.content : []

  const toolPairs = useMemo(() => pairTools(turnEvents), [turnEvents])
  const planEvents = turnEvents.filter((ev) => ev.type === 'ContextPlanComposed')
  const contextRecords = turnEvents.filter((ev) => ev.type === 'ContextContentRecorded')

  return (
    <div className="flex h-full flex-col">
      {/* TurnHeader */}
      <div className="shrink-0 space-y-1.5 border-b border-border px-4 py-3">
        <p className="font-mono text-[13px] font-medium text-ink">
          {turnLabel} · LLM round-trip
        </p>
        <div className="flex flex-wrap gap-1.5">
          {typeof started.model === 'string' && <Badge label="model" value={started.model} />}
          <Badge
            label="tokens"
            value={`${fmtTokens(usageInput(usage))}→${fmtTokens(usage?.output ?? 0)}`}
          />
          {usage != null && usage.cache_read > 0 && (
            <Badge label="cache" value={fmtTokens(usage.cache_read)} tone="accent" />
          )}
          {typeof finished.cost_usd === 'number' && finished.cost_usd > 0 && (
            <Badge label="cost" value={`$${finished.cost_usd.toFixed(4)}`} />
          )}
          {typeof finished.latency_ms === 'number' && (
            <Badge label="latency" value={fmtDuration(finished.latency_ms / 1000)} />
          )}
          {typeof response.stop_reason === 'string' && (
            <Badge label="stop" value={response.stop_reason} tone="accent" />
          )}
          {finished.success === false && <Badge value="request failed" tone="danger" />}
          {!round.finished && <Badge value="in progress…" />}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <LoadState loading={req.loading} error={req.error} />

        {systemText && (
          <Section title="system prompt" defaultOpen={false}>
            <ClampText text={systemText} />
          </Section>
        )}

        {tools.length > 0 && (
          <Section title="tools" badge={String(tools.length)} defaultOpen={false}>
            {tools.map((t, i) => (
              <ToolSpecRow key={i} spec={t} />
            ))}
          </Section>
        )}

        {messages.length > 0 && (
          <Section title="conversation" badge={String(messages.length)}>
            {messages.map((m, i) => (
              <MessageCard key={i} msg={(typeof m === 'object' && m !== null ? m : {}) as P} />
            ))}
          </Section>
        )}

        {(responseRef || resp.loading) && (
          <Section title="output">
            <LoadState loading={resp.loading} error={resp.error} />
            {outputBlocks.length > 0 && (
              // Mirrors noeta: output also goes through MessageCard, but open by
              // default (it is this turn's main artifact).
              <MessageCard msg={{ role: 'assistant', content: outputBlocks }} defaultOpen />
            )}
          </Section>
        )}

        {toolPairs.length > 0 && (
          <Section title="tool calls" badge={String(toolPairs.length)}>
            {toolPairs.map((p) => (
              <ToolPairCard key={p.callId} pair={p} />
            ))}
          </Section>
        )}

        {(planEvents.length > 0 || contextRecords.length > 0) && (
          <Section title="context provenance" defaultOpen={false}>
            {planEvents.map((ev) => {
              const ref = payloadOf(ev).plan_ref
              return isContentRef(ref) ? (
                <div key={ev.seq} className="space-y-1">
                  <PlanCompaction refJson={ref} />
                  <RefChip refJson={ref} label={`plan #${ev.seq}`} />
                </div>
              ) : null
            })}
            {contextRecords.map((ev) => {
              const p = payloadOf(ev)
              return (
                <p key={ev.seq} className="font-mono text-[11px] text-ink-2">
                  #{ev.seq} {String(p.kind)}/{String(p.name)} v{String(p.version)} · {String(p.policy)}
                </p>
              )
            })}
          </Section>
        )}

        <Section title={`raw payload · #${selected.seq} ${selected.type}`} defaultOpen={false}>
          <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
            {JSON.stringify(selected.payload, null, 2)}
          </pre>
        </Section>
      </div>
    </div>
  )
}
