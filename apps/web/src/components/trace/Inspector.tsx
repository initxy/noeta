/** Right-rail Inspector: session detail table + context stats + compaction overview
 *  + artifacts. The detail folds from the event stream where possible
 *  (task_id/agent_name/goal); status/model backfill from the session. Compactions
 *  are the debugging focus: macro compactions (Requested+Compacted pairs) and micro
 *  compactions (cleared_outputs from dereferenced plans) merge into one
 *  "compactions" section, each card clickable to jump to the matching timeline
 *  event; artifacts are just the output list, collapsed by default. */
import { useMemo, useState } from 'react'
import type { RawEnvelope, Session } from '../../api/types'
import { cn } from '../../lib/cn'
import { IconChevron } from '../icons'
import {
  collectCompactions,
  collectTaskExecution,
  fmtTokens,
  isContentRef,
  subagentLabel,
  type CompactionTrace,
  type ContentRefJson,
} from './model'
import { RefChip } from './RefChip'
import { useMicroCompactions, type MicroCompaction } from './useMicroCompactions'

type P = Record<string, unknown>

function payloadOf(ev: RawEnvelope): P {
  return typeof ev.payload === 'object' && ev.payload !== null ? (ev.payload as P) : {}
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-start justify-between gap-2 py-1 text-[12px]">
      <span className="shrink-0 text-ink-3">{label}</span>
      <span className="min-w-0 break-all text-right font-mono text-[11.5px] text-ink" title={String(value)}>
        {value}
      </span>
    </div>
  )
}

function Heading({ text }: { text: string }) {
  return (
    <p className="mb-1 mt-4 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-3 first:mt-0">
      {text}
    </p>
  )
}

function InlineBadge({ text }: { text: string }) {
  return (
    <span className="rounded-full border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-3">
      {text}
    </span>
  )
}

// ---- Compaction overview: macro + micro merged into one time series, one card per compaction ----

type CompactionCard =
  | { type: 'macro'; occurredAt: number; item: CompactionTrace }
  | { type: 'micro'; occurredAt: number; item: MicroCompaction }

function mergeCompactionCards(
  macros: CompactionTrace[],
  micros: MicroCompaction[],
): CompactionCard[] {
  return [
    ...macros.map((item): CompactionCard => ({ type: 'macro', occurredAt: item.occurredAt, item })),
    ...micros.map((item): CompactionCard => ({ type: 'micro', occurredAt: item.occurredAt, item })),
  ].sort((a, b) => a.occurredAt - b.occurredAt)
}

function CompactionCardView({
  card,
  owner,
  onJump,
}: {
  card: CompactionCard
  /** Ownership annotation for non-main streams (the subagent label). */
  owner: string | null
  onJump?: (taskId: string, seq: number) => void
}) {
  const macro = card.type === 'macro' ? card.item : null
  const micro = card.type === 'micro' ? card.item : null
  const taskId = macro?.taskId ?? micro!.taskId
  const seq = macro?.seq ?? micro!.seq
  const seqText = macro
    ? macro.compactedSeq !== null && macro.compactedSeq !== macro.seq
      ? `#${macro.seq}→#${macro.compactedSeq}`
      : `#${macro.seq}`
    : `#${micro!.seq}`
  const detail = macro
    ? [
        macro.compactedSeq === null ? 'never landed (request only)' : '',
        macro.replacedCount != null ? `folded ${macro.replacedCount} messages` : '',
        // A chars/4 reading, underestimating CJK/code by roughly 4x (the real
        // trigger works off blended real usage). Annotated so it is not read as
        // "how big the context was".
        macro.estimatedTokens != null
          ? `est. ${fmtTokens(macro.estimatedTokens)} tok (chars/4, low)`
          : '',
      ]
        .filter(Boolean)
        .join(' · ')
    : `cleared ${micro!.cleared} tool output${micro!.cleared === 1 ? '' : 's'}`
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className={cn('font-mono text-[11px]', macro ? 'text-warn' : 'text-accent')}>
          {seqText}
        </span>
        <InlineBadge text={macro ? macro.label : 'micro compaction · prune'} />
      </div>
      {owner && (
        <p className="mt-0.5 truncate font-mono text-[10px] text-ink-3" title={owner}>
          {owner}
        </p>
      )}
      {detail && (
        <p className="mt-0.5 truncate text-[11px] text-ink-2" title={detail}>
          {detail}
        </p>
      )}
    </>
  )
  const frame = cn(
    'w-full rounded-lg border px-2 py-1.5 text-left',
    macro ? 'border-warn/25 bg-warn-soft' : 'border-accent/25 bg-accent-soft',
  )
  if (!onJump) return <div className={frame}>{body}</div>
  return (
    <button
      type="button"
      onClick={() => onJump(taskId, seq)}
      className={cn(frame, 'transition-colors', macro ? 'hover:border-warn/50' : 'hover:border-accent/50')}
      title="Jump to the matching timeline event"
    >
      {body}
    </button>
  )
}

export function Inspector({
  events,
  session,
  onJump,
}: {
  events: RawEnvelope[]
  session: Session | null
  /** Jump to the timeline from a compaction card: taskId switches the scope first (seq collides across streams), seq locates the event. */
  onJump?: (taskId: string, seq: number) => void
}) {
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const info = useMemo(() => {
    let agentName = ''
    let goal = ''
    let model = ''
    let rounds = 0
    let toolCalls = 0
    let toolFailed = 0
    let questions = 0
    const artifacts: ContentRefJson[] = []
    const seen = new Set<string>()
    for (const ev of events) {
      const p = payloadOf(ev)
      switch (ev.type) {
        case 'TaskCreated':
          // The event stream contains subtask streams (each with its own
          // TaskCreated); the session header takes the first (the root comes first).
          if (!agentName && !goal) {
            agentName = String(p.agent_name ?? '')
            goal = String(p.goal ?? '')
          }
          break
        case 'LLMRequestStarted':
          rounds += 1
          if (typeof p.model === 'string') model = p.model
          break
        case 'ToolCallStarted':
          toolCalls += 1
          break
        case 'ToolResultRecorded': {
          if (p.success === false) toolFailed += 1
          const list = Array.isArray(p.artifacts) ? p.artifacts : []
          for (const a of list) {
            if (isContentRef(a) && !seen.has(a.hash)) {
              seen.add(a.hash)
              artifacts.push(a)
            }
          }
          break
        }
        case 'UserQuestionRequested':
          questions += 1
          break
      }
    }
    return { agentName, goal, model, rounds, toolCalls, toolFailed, questions, artifacts }
  }, [events])
  const execution = useMemo(() => collectTaskExecution(events), [events])
  const macroCompactions = useMemo(() => collectCompactions(events), [events])
  const microCompactions = useMicroCompactions(events)
  const compactionCards = useMemo(
    () => mergeCompactionCards(macroCompactions, microCompactions),
    [macroCompactions, microCompactions],
  )
  const ownerOf = useMemo(() => {
    const labels = new Map<string, string>()
    for (const sub of execution.subagents) labels.set(sub.id, subagentLabel(sub))
    return (taskId: string): string | null =>
      taskId === execution.mainTaskId ? null : (labels.get(taskId) ?? taskId)
  }, [execution])

  if (events.length === 0) {
    return <p className="p-4 text-center text-[12.5px] text-ink-3">No data yet.</p>
  }
  const last = events[events.length - 1]

  return (
    <div className="overflow-y-auto p-3">
      <Heading text="session" />
      <Row label="task_id" value={events[0].task_id} />
      {session?.status && <Row label="status" value={session.status} />}
      <Row label="model" value={info.model || session?.model || '—'} />
      {info.agentName && <Row label="agent" value={info.agentName} />}
      {info.goal && <Row label="goal" value={info.goal} />}
      <Row label="events" value={events.length} />
      <Row label="last_seq" value={last.seq} />

      <Heading text="context stats" />
      <Row label="LLM rounds" value={info.rounds} />
      <Row label="tool calls" value={info.toolCalls} />
      {info.toolFailed > 0 && <Row label="tool failures" value={info.toolFailed} />}
      {execution.subagents.length > 0 && <Row label="subagents" value={execution.subagents.length} />}
      {compactionCards.length > 0 && (
        <Row
          label="compactions"
          value={[
            macroCompactions.length > 0 ? `macro ${macroCompactions.length}` : '',
            microCompactions.length > 0 ? `micro ${microCompactions.length}` : '',
          ]
            .filter(Boolean)
            .join(' · ')}
        />
      )}
      {info.questions > 0 && <Row label="user questions" value={info.questions} />}

      {execution.subagents.length > 0 && (
        <>
          <Heading text="subagents" />
          <div className="space-y-1.5">
            {execution.subagents.map((subagent) => (
              <div key={subagent.id} className="rounded-lg border border-border bg-surface p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[12px] font-medium text-ink" title={subagent.goal}>
                    {subagentLabel(subagent)}
                  </span>
                  <InlineBadge text={subagent.status} />
                </div>
                <p className="mt-0.5 truncate text-[11px] text-ink-3" title={subagent.goal || subagent.summary}>
                  {subagent.goal || subagent.summary || subagent.id}
                </p>
                <p className="mt-1 font-mono text-[10px] text-ink-3">
                  seq {subagent.startSeq ?? '—'} → {subagent.endSeq ?? '…'} · {subagent.eventCount} events
                </p>
              </div>
            ))}
          </div>
        </>
      )}

      {compactionCards.length > 0 && (
        <>
          <Heading text={`compactions · ${compactionCards.length}`} />
          <div className="space-y-1.5">
            {compactionCards.map((card) => (
              <CompactionCardView
                key={`${card.type}-${card.item.taskId}-${card.item.seq}`}
                card={card}
                owner={ownerOf(card.item.taskId)}
                onJump={onJump}
              />
            ))}
          </div>
        </>
      )}

      {info.artifacts.length > 0 && (
        <>
          {/* The artifact list is only for when it's needed: collapsed by default so
              a long list doesn't push the compaction section out of view. */}
          <button
            type="button"
            onClick={() => setArtifactsOpen((v) => !v)}
            className="mt-4 flex w-full items-center gap-1.5 text-left"
          >
            <IconChevron open={artifactsOpen} className="h-3 w-3 shrink-0 text-ink-3" />
            <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-3">
              artifacts · {info.artifacts.length}
            </span>
          </button>
          {artifactsOpen && (
            <div className="mt-1 space-y-1.5">
              {info.artifacts.map((a) => (
                <RefChip key={a.hash} refJson={a} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
