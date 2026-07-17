import { useEffect, useMemo, useRef, useState } from 'react'
import type { AnswerPayload, ResolvedKnowledgePath } from '../api/types'
import {
  type ChatItem,
  type StepItem,
  type SubtaskItem,
} from '../chat/useChat'
import { type StreamingBlock } from '../chat/streaming'
import { useResolvedKnowledgePaths } from '../chat/useCitations'
import { cn } from '../lib/cn'
import { feedbackApi } from '../api/endpoints'
import { ReferencesFooter } from './Citations'
import { FeedbackControls } from './FeedbackControls'
import { IconChevron } from './icons'
import {
  argsPreview,
  buildNodes,
  buildTimeline,
  collectCitationRefs,
  isBackgroundNotice,
  MEMORY_OP_LABELS,
  runningStatus,
  type TimelineEntry,
} from './conversationNodes'
import { Markdown } from './Markdown'
import { QuestionCard } from './QuestionCard'

interface ConversationProps {
  items: ChatItem[]
  running: boolean
  connected: boolean
  connectionError: string | null
  /** Space owning the session: used by the citation resolve-paths endpoint (references do not render when null) */
  spaceId?: string | null
  /** Session id: anchors message-level 👍👎 feedback (null/undefined = draft state, no feedback controls) */
  sessionId?: string | null
  /** Task id of the current workflow tab (unset for ordinary sessions; the backend falls back to session.task_id) */
  taskId?: string | null
  /** Current user's avatar URL (falls back to the initial letter) */
  userAvatar?: string
  /** Current user's display name (avatar fallback takes its first letter) */
  userName?: string
  /** Live token-streaming preview blocks (null = no preview). Deltas are a transient projection; they never enter items. */
  streamingBlocks: StreamingBlock[] | null
  onAnswer: (questionId: string, answers: AnswerPayload) => Promise<void>
  /** Open a citation origin link (a new browser tab in this port). */
  onOpenDoc: (url: string) => void
  /** Workspace-relative file paths: file-looking text in the body renders as file chips */
  workspaceFiles: string[]
  /** Clicking a file chip: opens the preview in the side panel's "Workspace files" tab */
  onOpenFile: (path: string) => void
}

/** Avatar: image first; falls back to the initial letter when missing or failing to load. 32px circle. */
function Avatar({ src, name }: { src?: string; name?: string }) {
  const [err, setErr] = useState(false)
  if (src && !err) {
    return (
      <img
        src={src}
        alt={name || 'User'}
        onError={() => setErr(true)}
        className="h-8 w-8 shrink-0 rounded-full object-cover"
      />
    )
  }
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[13px] font-medium uppercase text-ink">
      {(name || '?').charAt(0)}
    </span>
  )
}

/** Agent avatar: brand-color circle + the "N" letter mark (echoing the Logo); a glow pulse while running (the only running animation). */
function AgentAvatar({ running = false }: { running?: boolean }) {
  return (
    <span
      className={cn(
        'flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-soft font-mono text-[15px] font-semibold text-accent',
        running && 'agent-avatar--running',
      )}
      aria-hidden
    >
      N
    </span>
  )
}

/* ============================================================
 * Execution-process area (refactor C1): single collapsible container +
 * a vertical rail timeline when expanded.
 *
 * Design intent:
 * - Collapsed: see at a glance "what was done / how many steps / where it is
 *   stuck now"; while running the status bar (runningStatus) reflects the
 *   current action live; failures do not surface in the collapsed state — it
 *   presents identically to an all-success run.
 * - Expanded: a clean rail timeline — a 2px grey track + status dots on the
 *   left, aligned compact rows of "tool name + one-line args preview" on the
 *   right, each expandable to full args/output (pre + monospace + horizontal
 *   scroll). thinking is not shown; skill/memory get small markers; a subtask
 *   is a first-class expandable card opening to its summary + tool steps.
 * ============================================================ */

const SUBTASK_STATUS: Record<
  SubtaskItem['status'],
  { label: string }
> = {
  running: { label: 'Working' },
  completed: { label: 'Done' },
  failed: { label: 'Failed' },
  cancelled: { label: 'Stopped' },
}

/** Status-dot color/animation: success = grey dot (subdued once done), running = accent pulse, failed = danger. */
function RailDot({ state }: { state: 'done' | 'running' | 'failed' }) {
  if (state === 'running') return <span className="rail-dot rail-dot--active shrink-0" aria-hidden />
  if (state === 'failed') return <span className="rail-dot shrink-0 bg-danger" aria-hidden />
  return <span className="rail-dot rail-dot--done shrink-0" aria-hidden />
}

/** Long content like tool output/args: monospace pre + horizontal scroll, red background on failure. */
function CodeBlock({
  children,
  tone = 'default',
  maxHeight = '16rem',
}: {
  children: React.ReactNode
  tone?: 'default' | 'danger'
  maxHeight?: string
}) {
  return (
    <pre
      className={cn(
        'mt-1.5 overflow-x-auto rounded-lg p-2.5 font-mono text-[11.5px] leading-relaxed',
        tone === 'danger' ? 'bg-danger-soft text-danger' : 'bg-surface-2 text-ink-2',
      )}
      style={{ maxHeight }}
    >
      {children}
    </pre>
  )
}

/** Single-row tool step: status dot + tool name + inline args preview + chevron; click to expand details. */
function StepRow({ step }: { step: StepItem }) {
  const [open, setOpen] = useState(false)
  const preview = argsPreview(step.args, 60)
  const result = step.result
  const isRunning = result === null
  const failed = !isRunning && !result.success
  const state: 'running' | 'failed' | 'done' = isRunning ? 'running' : failed ? 'failed' : 'done'
  const label = isRunning ? 'Running' : failed ? 'Failed' : 'Done'

  return (
    <div className="relative pl-5">
      {/* Rail vertical line */}
      <span
        className="absolute left-[4px] top-0 h-full w-px bg-border"
        aria-hidden
      />
      <span className="absolute left-0 top-1">
        <RailDot state={state} />
      </span>
      {isRunning ? (
        <div
          className="flex items-center gap-2 py-1 text-[12.5px]"
          title={label}
        >
          <span className="font-mono text-accent">{step.toolName}</span>
          {preview && (
            <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-ink-3">
              {preview}
            </span>
          )}
        </div>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="group flex w-full items-center gap-2 py-1 text-left"
          >
            <span
              className={cn(
                'shrink-0 font-mono text-[12px]',
                failed ? 'font-medium text-danger' : 'text-ink-2',
              )}
            >
              {step.toolName}
            </span>
            {preview && (
              <span
                className={cn(
                  'min-w-0 flex-1 truncate font-mono text-[11.5px]',
                  failed ? 'text-danger/80' : 'text-ink-3',
                )}
              >
                {preview}
              </span>
            )}
            {failed && result.summary && (
              <span className="ml-1 shrink-0 truncate text-[11.5px] text-danger/90">
                {result.summary}
              </span>
            )}
            <IconChevron
              open={open}
              className="h-3 w-3 shrink-0 text-ink-3 opacity-0 transition-opacity group-hover:opacity-100"
            />
          </button>
          {open && (
            <div className="mb-2 space-y-1.5 pr-1">
              <div>
                <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
                  Arguments
                </p>
                <CodeBlock>{JSON.stringify(step.args, null, 2)}</CodeBlock>
              </div>
              {(result.output || result.summary) && (
                <div>
                  <p
                    className={cn(
                      'font-mono text-[10.5px] uppercase tracking-[0.12em]',
                      failed ? 'text-danger' : 'text-ink-3',
                    )}
                  >
                    {failed ? `Failed: ${result.summary || 'Unknown error'}` : 'Output'}
                  </p>
                  <CodeBlock tone={failed ? 'danger' : 'default'}>
                    {result.output || result.summary || '(no output)'}
                  </CodeBlock>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

/** Small marker row for skills / memory etc. (no expansion). */
function MarkerRow({ label, text }: { label: string; text: string }) {
  return (
    <div className="relative pl-5">
      <span
        className="absolute left-[4px] top-0 h-full w-px bg-border"
        aria-hidden
      />
      <span className="absolute left-0 top-1">
        <RailDot state="done" />
      </span>
      <p className="py-1 font-mono text-[11.5px] text-ink-3">
        {label} <span className="text-ink-2">{text}</span>
      </p>
    </div>
  )
}

/**
 * Subtask card: a first-class expandable object inside the process detail.
 * The header is a status dot + agent name (accent) + goal (single-line
 * truncated collapsed, fully wrapped expanded) + a status badge; expanded, the
 * same rail first shows the subtask summary (the result, visible in every
 * terminal state — not just failed; success renders as Markdown, since subtask
 * results are usually markdown with tables), then the subtask's internal tool
 * steps. The result appears only inside this card, never in the main process body.
 */
function SubtaskCard({
  item,
  onOpenDoc,
}: {
  item: SubtaskItem
  onOpenDoc: (url: string) => void
}) {
  const [open, setOpen] = useState(item.status === 'running')
  const status = SUBTASK_STATUS[item.status]
  const dotState: 'running' | 'failed' | 'done' =
    item.status === 'running' ? 'running' : item.status === 'failed' ? 'failed' : 'done'
  // The subtask's internal tool steps (auto-expanded while running, easy to observe)
  const steps = item.steps
  const hasSummary = item.summary.trim().length > 0
  const expandable = hasSummary || steps.length > 0
  return (
    <div className="relative pl-5">
      <span
        className="absolute left-[4px] top-0 h-full w-px bg-border"
        aria-hidden
      />
      <span className="absolute left-0 top-[9px]">
        <RailDot state={dotState} />
      </span>
      <div className="rounded-lg border border-border/70 bg-surface-2/60 px-2.5 py-1.5 my-1">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={cn(
            'flex w-full gap-2 text-left',
            // Expanded, the goal wraps fully and other elements top-align; collapsed single line centers
            open ? 'items-start' : 'items-center',
          )}
        >
          <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
            Subtask
          </span>
          <span className="shrink-0 font-mono text-[11.5px] text-accent">
            {item.agentName}
          </span>
          <span
            className={cn(
              'min-w-0 flex-1 text-[12px] text-ink-2',
              open ? 'whitespace-pre-wrap break-words' : 'truncate',
            )}
            title={item.goal}
          >
            {item.goal}
          </span>
          <span
            className={cn(
              'shrink-0 font-mono text-[10.5px]',
              dotState === 'running' && 'text-accent',
              dotState === 'failed' && 'text-danger',
              dotState === 'done' && 'text-ink-3',
            )}
          >
            {status.label}
          </span>
          {expandable && (
            <IconChevron open={open} className="h-3 w-3 shrink-0 text-ink-3" />
          )}
        </button>
        {open && expandable && (
          <div className="mt-2 space-y-2 border-l border-border/80 pl-3">
            {hasSummary && (
              <div>
                <p className="mb-0.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
                  Result
                </p>
                {item.status === 'failed' ? (
                  <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-danger">
                    {item.summary}
                  </p>
                ) : (
                  <div className="text-[13px] leading-relaxed text-ink-2">
                    <Markdown text={item.summary} onOpenDoc={onOpenDoc} />
                  </div>
                )}
              </div>
            )}
            {steps.length > 0 && (
              <div>
                {steps.map((s, i) => (
                  <SubtaskStepRow key={s.callId + i} step={s} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/** Tool step inside a subtask: reuses the StepRow visuals but more compact (smaller type, no rail overlap). */
function SubtaskStepRow({ step }: { step: StepItem }) {
  const [open, setOpen] = useState(false)
  const preview = argsPreview(step.args, 50)
  const result = step.result
  const isRunning = result === null
  const failed = !isRunning && !result.success
  return (
    <div className="py-0.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="group flex w-full items-center gap-2 text-left"
      >
        <span
          className={cn(
            'rail-dot shrink-0',
            isRunning && 'rail-dot--active',
            !isRunning && failed && 'bg-danger',
            !isRunning && !failed && 'rail-dot--done',
          )}
          aria-hidden
        />
        <span
          className={cn(
            'shrink-0 font-mono text-[11px]',
            failed ? 'text-danger' : isRunning ? 'text-accent' : 'text-ink-2',
          )}
        >
          {step.toolName}
        </span>
        {preview && (
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-3">
            {preview}
          </span>
        )}
      </button>
      {open && result && (result.output || result.summary) && (
        <CodeBlock tone={failed ? 'danger' : 'default'} maxHeight="10rem">
          {result.output || result.summary}
        </CodeBlock>
      )}
    </div>
  )
}

/** Intermediate assistant body (staged output within the tool loop): inline on the rail, de-emphasized. */
function MidAssistantRow({
  text,
  onOpenDoc,
  workspaceFiles,
  onOpenFile,
  citations,
}: {
  text: string
  onOpenDoc: (url: string) => void
  workspaceFiles: string[]
  onOpenFile: (path: string) => void
  citations?: Map<string, ResolvedKnowledgePath>
}) {
  return (
    <div className="relative pl-5">
      <span
        className="absolute left-[4px] top-0 h-full w-px bg-border"
        aria-hidden
      />
      <span className="absolute left-0 top-1">
        <RailDot state="done" />
      </span>
      <div className="py-1.5 pr-1 text-[13px] leading-relaxed text-ink-2">
        <Markdown
          text={text}
          onOpenDoc={onOpenDoc}
          workspaceFiles={workspaceFiles}
          onOpenFile={onOpenFile}
          citations={citations}
        />
      </div>
    </div>
  )
}

/**
 * Execution-process container: collapse button + rail timeline when expanded.
 *
 * Collapsed: rail-dot status + "Process details · …" (runningStatus / a
 * completion summary); expanded: a border-top separates it from the body,
 * below it a unified rail timeline where every step/skill/memory/subtask/
 * intermediate assistant aligns along the rail (thinking is not shown).
 */
function ProcessContainer({
  items,
  running,
  live,
  onAnswer,
  onOpenDoc,
  workspaceFiles,
  onOpenFile,
  citations,
}: {
  items: ChatItem[]
  running: boolean
  /** Past replay, in the live state (connected): only a live in-progress turn auto-expands */
  live: boolean
  onAnswer: (questionId: string, answers: AnswerPayload) => Promise<void>
  onOpenDoc: (url: string) => void
  workspaceFiles: string[]
  onOpenFile: (path: string) => void
  citations?: Map<string, ResolvedKnowledgePath>
}) {
  // Collapsed by default: only a live in-progress turn auto-expands, folding
  // back 1.5s after it finishes. During replay / session switches live=false
  // and it stays collapsed — no "expand then auto-collapse" flash.
  const [open, setOpen] = useState(live && running)
  const userToggled = useRef(false)
  useEffect(() => {
    if (!live) return undefined
    if (running) {
      userToggled.current = false
      setOpen(true)
      return undefined
    }
    if (userToggled.current) return undefined
    const t = window.setTimeout(() => setOpen(false), 1500)
    return () => window.clearTimeout(t)
  }, [running, live])

  const stepCount = items.filter(
    (it) => it.kind === 'step' || it.kind === 'subtask',
  ).length
  // The collapsed label is uniformly "Process details · …": runningStatus while
  // running, the step count once finished. Failures do not surface collapsed —
  // no count, no red; identical to an all-success run (failure detail remains
  // visible step by step when expanded).
  const statusLabel = running
    ? `Process details · ${runningStatus(items)}`
    : `Process details · ${
        stepCount > 0 ? `${stepCount} step${stepCount === 1 ? '' : 's'}` : 'execution log'
      }`

  const entries = buildTimeline(items)
  // The data layer still keeps groupTimeline/activitySummary (alternative
  // grouping logic, covered by Conversation.test); the current render lays the
  // timeline entries out linearly — assistant/question insert as blocks, the
  // rail provides the unified visual track, and no extra ActivityGroup folding
  // is applied. Those two functions can be reused if folding returns later.
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          userToggled.current = true
          setOpen((v) => !v)
        }}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left transition-colors',
          'hover:bg-surface-2',
        )}
      >
        <span
          className={cn(
            'rail-dot shrink-0',
            running ? 'rail-dot--active' : 'rail-dot--done',
          )}
          aria-hidden
        />
        <span
          className={cn(
            'min-w-0 flex-1 truncate text-[12.5px]',
            running ? 'text-ink-2' : 'text-ink-3',
          )}
        >
          {statusLabel}
        </span>
        <IconChevron open={open} className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      </button>
      {open && (
        <div className="mt-1.5 border-t border-border/60 pt-2">
          <ProcessRail
            entries={entries}
            onAnswer={onAnswer}
            onOpenDoc={onOpenDoc}
            workspaceFiles={workspaceFiles}
            onOpenFile={onOpenFile}
            citations={citations}
          />
        </div>
      )}
    </div>
  )
}

/** Expanded state: lay all timeline entries along the rail; assistant/question insert as "blocks". */
function ProcessRail({
  entries,
  onAnswer,
  onOpenDoc,
  workspaceFiles,
  onOpenFile,
  citations,
}: {
  entries: TimelineEntry[]
  onAnswer: (questionId: string, answers: AnswerPayload) => Promise<void>
  onOpenDoc: (url: string) => void
  workspaceFiles: string[]
  onOpenFile: (path: string) => void
  citations?: Map<string, ResolvedKnowledgePath>
}) {
  // buildTimeline already dropped thinking at the data layer (the thought
  // process is not shown); lay out linearly here: blocks (assistant/question)
  // get natural spacing, no nested ActivityGroup.
  return (
    <div className="pl-1">
      {entries.map((e, i) => {
        switch (e.kind) {
          case 'step':
            return <StepRow key={e.step.callId} step={e.step} />
          case 'skill':
            return <MarkerRow key={i} label="Skill ·" text={e.skill} />
          case 'memory':
            return (
              <MarkerRow
                key={i}
                label={`${MEMORY_OP_LABELS[e.op]} ·`}
                text={e.name}
              />
            )
          case 'subtask':
            return <SubtaskCard key={i} item={e.item} onOpenDoc={onOpenDoc} />
          case 'assistant':
            return (
              <MidAssistantRow
                key={i}
                text={e.text}
                onOpenDoc={onOpenDoc}
                workspaceFiles={workspaceFiles}
                onOpenFile={onOpenFile}
                citations={citations}
              />
            )
          case 'question':
            // An answered question inside the process timeline: render directly, indented into the rail
            return (
              <div key={i} className="relative pl-5">
                <span
                  className="absolute left-[4px] top-0 h-full w-px bg-border"
                  aria-hidden
                />
                <span className="absolute left-0 top-1">
                  <RailDot state="done" />
                </span>
                <div className="py-1 pr-1">
                  <QuestionCard
                    item={{ ...e.item, answered: true }}
                    onSubmit={onAnswer}
                  />
                </div>
              </div>
            )
          default:
            return null
        }
      })}
    </div>
  )
}

/**
 * Live token-streaming bubble (transient projection): rendered at the end of
 * the message flow while running, its body styled like the assistant Markdown
 * body; thinking blocks are de-emphasized (text-ink-3, 12.5px, no folding —
 * the live stream stays expanded to show progress). Once the durable
 * assistant_text lands the streaming buffer clears and the bubble disappears —
 * no duplication, no flicker. Multiple index blocks concatenate in order.
 */
function StreamingBubble({ blocks }: { blocks: StreamingBlock[] }) {
  return (
    <li className="msg-enter" aria-label="AI reply (live)">
      <div className="flex gap-3">
        <AgentAvatar running />
        <div className="min-w-0 flex-1 space-y-1.5">
          {blocks.map((b) =>
            b.kind === 'text' ? (
              <div
                key={b.index}
                className="whitespace-pre-wrap break-words text-[14.5px] leading-relaxed text-ink"
              >
                {b.text}
                <span className="streaming-caret" aria-hidden />
              </div>
            ) : (
              <p
                key={b.index}
                className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-3"
              >
                {b.text}
              </p>
            ),
          )}
        </div>
      </div>
    </li>
  )
}

/**
 * Conversation flow: turns are bounded by user messages, separated by divider
 * lines. User messages and agent replies both use the "avatar + content"
 * horizontal layout; process content folds into the execution-process
 * container, indented to the AI content column, collapsed by default.
 */
export function Conversation({
  items,
  running,
  connected,
  connectionError,
  spaceId,
  sessionId,
  taskId,
  userAvatar,
  userName,
  streamingBlocks,
  onAnswer,
  onOpenDoc,
  workspaceFiles,
  onOpenFile,
}: ConversationProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [follow, setFollow] = useState(true)

  // Message-level feedback (ADR-0017): seq → submitted rating. Pull the
  // session's existing feedback once on mount to mark "feedback sent";
  // workflow sessions filter by the current tab's task (seq counts per task).
  const [feedbackBySeq, setFeedbackBySeq] = useState<Map<number, number>>(
    () => new Map(),
  )
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    feedbackApi
      .listForSession(sessionId)
      .then(({ feedback }) => {
        if (cancelled) return
        const map = new Map<number, number>()
        for (const fb of feedback) {
          if (fb.event_seq == null) continue
          if (taskId && fb.task_id && fb.task_id !== taskId) continue
          map.set(fb.event_seq, fb.rating)
        }
        setFeedbackBySeq(map)
      })
      .catch(() => {
        /* Failing to load feedback markers never affects the conversation */
      })
    return () => {
      cancelled = true
    }
  }, [sessionId, taskId])
  const markFeedback = (seq: number, rating: 1 | -1) =>
    setFeedbackBySeq((cur) => new Map(cur).set(seq, rating))

  // Feedback-control mount points: the seq of each turn's (bounded by user
  // messages) last assistant message. In-progress turns get none (the result
  // is not final); background notices do not count as replies.
  const feedbackSeqs = useMemo(() => {
    const set = new Set<number>()
    let lastAssistant: number | null = null
    for (const it of items) {
      if (it.kind === 'user') {
        if (lastAssistant != null) set.add(lastAssistant)
        lastAssistant = null
      } else if (it.kind === 'assistant' && !isBackgroundNotice(it.text)) {
        lastAssistant = it.seq
      }
    }
    if (lastAssistant != null && !running) set.add(lastAssistant)
    return set
  }, [items, running])

  // Citation provenance: collect the session's knowledge/ paths awaiting
  // resolve (body footnotes ∪ tool consultations) and resolve in batches;
  // footerBySeq locates the assistant seq each turn's "references" bar mounts on.
  const { allRaws, footerBySeq } = useMemo(
    () => collectCitationRefs(items, running),
    [items, running],
  )
  const citations = useResolvedKnowledgePaths(spaceId, allRaws)

  // 200ms debounce on the loading hint: fast replays (usually tens of ms) never show it — no flicker
  const loading = !connected && items.length === 0 && !connectionError
  const [loadingVisible, setLoadingVisible] = useState(false)
  useEffect(() => {
    if (!loading) {
      setLoadingVisible(false)
      return
    }
    const timer = window.setTimeout(() => setLoadingVisible(true), 200)
    return () => window.clearTimeout(timer)
  }, [loading])

  useEffect(() => {
    if (follow && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [items, running, streamingBlocks, follow])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 80)
  }

  const nodes = buildNodes(items, running)
  // Whether this turn has no visible render node yet (the last node is a user
  // message, or the list is empty). thinking / todos are transparently skipped
  // in buildNodes and produce no node, so a pure-thinking running state shows
  // neither an empty process detail nor a gap — it lands on the trailing
  // "Thinking…" placeholder (not shown while streaming has content).
  const lastNode = nodes[nodes.length - 1]
  const turnHasNoVisibleNode =
    !lastNode ||
    (lastNode.kind === 'item' && lastNode.item.kind === 'user')

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-y-auto"
        aria-live="polite"
      >
        <div className="mx-auto max-w-3xl px-4 pb-8 pt-6 sm:px-6">
          {loading && loadingVisible && (
            <p className="pt-10 text-[13px] text-ink-3">Loading…</p>
          )}

          <ul className="space-y-4">
            {/* Whether this turn (since the last user message) has shown an
                assistant body: the first block carries the agent avatar, later
                same-turn body blocks only indent to align (grouping) */}
            {(() => {
              let seenAssistantInTurn = false
              return nodes.map((node, idx) => {
              // Execution-process container: indented to the AI content column (avatar 32px + gap 12px = 44px)
              if (node.kind === 'container') {
                return (
                  <li key={`container-${node.seq}`} className="msg-enter pl-11">
                    <ProcessContainer
                      items={node.items}
                      running={node.running}
                      live={connected}
                      onAnswer={onAnswer}
                      onOpenDoc={onOpenDoc}
                      workspaceFiles={workspaceFiles}
                      onOpenFile={onOpenFile}
                      citations={citations}
                    />
                  </li>
                )
              }

              const item = node.item
              if (item.kind === 'user') {
                seenAssistantInTurn = false
                return (
                  <li key={item.seq} className="msg-enter">
                    {/* Turn divider */}
                    {idx > 0 && (
                      <div className="mb-4 mt-2 border-t border-border" aria-hidden />
                    )}
                    <div className="flex gap-3">
                      <Avatar src={userAvatar} name={userName} />
                      <div className="min-w-0 flex-1">
                        <p className="w-fit max-w-full whitespace-pre-wrap break-words rounded-2xl bg-surface-2 px-3.5 py-2 text-[14.5px] font-medium leading-relaxed text-ink">
                          {item.content}
                        </p>
                      </div>
                    </div>
                  </li>
                )
              }

              // AI body: the turn's first block carries the agent avatar; later same-turn body blocks only indent to align (grouping)
              if (item.kind === 'assistant') {
                const grouped = seenAssistantInTurn
                seenAssistantInTurn = true
                // The turn's last assistant with the turn finished: mount the collapsible references bar underneath
                const footerRefs = footerBySeq.get(item.seq)
                const footer = footerRefs ? (
                  <ReferencesFooter
                    entries={footerRefs.map((e) => ({
                      ...e,
                      resolved: citations.get(e.raw),
                    }))}
                    onOpenDoc={onOpenDoc}
                  />
                ) : null
                // Feedback controls (👍👎): only on the last assistant of a
                // finished turn — rating every intermediate progress message is meaningless.
                const feedbackCtl =
                  sessionId && feedbackSeqs.has(item.seq) ? (
                    <FeedbackControls
                      sessionId={sessionId}
                      taskId={taskId}
                      seq={item.seq}
                      submittedRating={feedbackBySeq.get(item.seq)}
                      onSubmitted={markFeedback}
                    />
                  ) : null
                if (grouped) {
                  return (
                    <li key={item.seq} className="msg-enter group/msg pl-11">
                      <Markdown
                        text={item.text}
                        onOpenDoc={onOpenDoc}
                        workspaceFiles={workspaceFiles}
                        onOpenFile={onOpenFile}
                        citations={citations}
                      />
                      {footer}
                      {feedbackCtl}
                    </li>
                  )
                }
                return (
                  <li key={item.seq} className="msg-enter group/msg" aria-label="AI reply">
                    <div className="flex gap-3">
                      <AgentAvatar />
                      <div className="min-w-0 flex-1">
                        <Markdown
                          text={item.text}
                          onOpenDoc={onOpenDoc}
                          workspaceFiles={workspaceFiles}
                          onOpenFile={onOpenFile}
                          citations={citations}
                        />
                        {footer}
                        {feedbackCtl}
                      </div>
                    </div>
                  </li>
                )
              }

              // Checklist: not rendered in the main flow (it would scroll away
              // with messages) — the persistent display moved to the TodoStrip
              // above the composer (App takes the latest todos snapshot from items).
              if (item.kind === 'todos') return null

              // Unanswered question / error / turn terminal state / compaction divider: persistent in the main flow, indented to align
              return (
                <li key={item.seq} className="msg-enter pl-11">
                  {item.kind === 'question' && (
                    <QuestionCard item={item} onSubmit={onAnswer} />
                  )}
                  {item.kind === 'error' && (
                    <div className="rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
                      {item.message}
                    </div>
                  )}
                  {item.kind === 'turn_end' && (
                    <p className="font-mono text-[11.5px] text-ink-3">
                      {item.status === 'cancelled' ? '— Stopped —' : '— This turn failed —'}
                    </p>
                  )}
                  {item.kind === 'compaction' && (
                    <p className="font-mono text-[11.5px] text-ink-3">
                      {item.replaced > 0
                        ? `— Context compacted; ${item.replaced} earlier message${
                            item.replaced === 1 ? '' : 's'
                          } folded away —`
                        : '— Context compacted —'}
                    </p>
                  )}
                </li>
              )
              })
            })()}

            {/* End of the running state: streaming content → live bubble (the
                durable assistant_text clears streaming, so no duplication with
                the body). With no delta and no visible node this turn, fall back
                to the Thinking… placeholder (before the first token / a pure
                thinking segment); once a process container or body appears they
                show the progress and this placeholder is not repeated. */}
            {running && streamingBlocks && streamingBlocks.length > 0 && (
              <StreamingBubble blocks={streamingBlocks} />
            )}
            {running && !streamingBlocks?.length && turnHasNoVisibleNode && (
              <li className="msg-enter flex items-center gap-3" aria-label="AI reply">
                <AgentAvatar running />
                <span className="text-[13px] text-ink-3">Thinking…</span>
              </li>
            )}
          </ul>
        </div>
      </div>

      {!follow && (
        <button
          type="button"
          onClick={() => {
            setFollow(true)
            scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
          }}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-border bg-surface px-3.5 py-1.5 text-[12px] text-ink-2 shadow-[var(--shadow)] transition-colors hover:text-ink"
        >
          ↓ Back to bottom
        </button>
      )}
    </div>
  )
}
