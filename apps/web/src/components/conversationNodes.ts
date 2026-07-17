import {
  type ChatItem,
  type QuestionItemView,
  type StepItem,
  type SubtaskItem,
} from '../chat/useChat'
import { type MemoryOp } from '../api/types'
import { extractKnowledgePaths, parseCitationDefs } from '../lib/citations'

/** The "visible" process-item kinds folded into a turn's execution container.
 * - thinking is excluded: the thought process is not shown and does not enter
 *   container.items — otherwise a pure-thinking turn would fold into an empty process
 *   detail; the data stays in items, only the render layer hides it.
 * - todos is excluded: the checklist is progress information, permanently shown in the
 *   TodoStrip above the composer.
 * Whether an assistant message is intermediate (folded) is decided by "the last
 * visible process event", independent of thinking. */
const PROCESS_KINDS = new Set<ChatItem['kind']>([
  'skill',
  'step',
  'memory',
  'subtask',
])

/**
 * Background-subtask result feed-back notice: `[subagent] Result from <agent> ...`.
 * This is origin=system text the host injects for the model (ADR-0004); the subtask
 * summary reaches its card via subtask_finished, so the main process body must not
 * repeat it. The backend already filters user messages by origin, but legacy data or
 * intermediate assistant text may still contain it; the frontend recognizes the prefix
 * and skips rendering (no protocol change).
 */
export function isBackgroundNotice(text: string): boolean {
  return /^\s*\[subagent\]\s+Result from /.test(text)
}

/** Render node: process items are folded into a container. */
export type RenderNode =
  | { kind: 'item'; item: Exclude<ChatItem, StepItem> }
  | { kind: 'container'; seq: number; items: ChatItem[]; running: boolean }

/**
 * Fold items into render nodes: segments split by user messages; within a segment the
 * visible process items (skill/step/memory/subtask/answered question) and any assistant
 * text before "the last visible process event" merge into the container; assistant text
 * after it is the final result and stays in the main flow. thinking / todos / background
 * notices are transparently skipped: they neither fold into the container nor stay in
 * the main flow, so a pure-thinking segment produces no empty container.
 * A trailing container that is the last node of the stream while still running is
 * marked running to render the pulsing status bar.
 */
export function buildNodes(
  items: ChatItem[],
  running = false,
): RenderNode[] {
  const n = items.length
  // Pass 1: segmentation (split on user), recording the index of each segment's last
  // process item — used to decide whether an assistant message is intermediate
  // (folded) or the final result (main flow).
  const segLastProc = new Map<number, number>()
  let curSeg = 0
  for (let i = 0; i < n; i++) {
    const item = items[i]
    if (i > 0 && item.kind === 'user') curSeg += 1
    const isProc =
      PROCESS_KINDS.has(item.kind) ||
      (item.kind === 'question' && item.answered)
    if (isProc) segLastProc.set(curSeg, i)
  }

  // Pass 2: emit nodes. Process items accumulate into the container; main-flow items
  // flush the container first, then emit.
  const nodes: RenderNode[] = []
  let container: { seq: number; items: ChatItem[] } | null = null
  curSeg = 0

  const isProcessAt = (item: ChatItem, i: number, seg: number) => {
    const lastProc = segLastProc.get(seg) ?? -1
    if (PROCESS_KINDS.has(item.kind)) return true
    if (item.kind === 'question') return item.answered
    if (item.kind === 'assistant') return i <= lastProc
    return false
  }

  for (let i = 0; i < n; i++) {
    const item = items[i]
    if (i > 0 && item.kind === 'user') curSeg += 1

    // thinking is never shown: neither folded into the container nor kept in the main
    // flow — a pure-thinking segment therefore yields no empty process container (the
    // data stays in items for runningStatus etc.; it just does not render).
    if (item.kind === 'thinking') continue
    // todos is progress information; it does not enter the main-flow cards (it lives in
    // the TodoStrip above the composer) and must not break the execution container —
    // transparently skipped so a turn keeps a single process container.
    if (item.kind === 'todos') continue
    // Background-subtask result notices are not shown as main process body / final
    // result (the result belongs to the corresponding subtask card).
    if (item.kind === 'assistant' && isBackgroundNotice(item.text)) continue

    if (isProcessAt(item, i, curSeg)) {
      if (!container) container = { seq: item.seq, items: [] }
      container.items.push(item)
    } else {
      if (container) {
        nodes.push({
          kind: 'container',
          seq: container.seq,
          items: container.items,
          running: false,
        })
        container = null
      }
      // Only main-flow items reach this branch (user / final assistant / unanswered
      // question / error / turn_end / compaction): thinking/todos/notice continued
      // above and isProcessAt filtered the process kinds, so the type narrows here.
      // compaction is a context boundary and stays in the main flow as a divider
      // (cutting the container when it lands mid-process is intended — the content
      // before and after compaction are two different contexts).
      nodes.push({ kind: 'item', item: item as Exclude<ChatItem, StepItem> })
    }
  }

  // Trailing container: reaching here means it is the last node of the stream; if
  // still running, mark running to render the pulsing status bar.
  if (container) {
    nodes.push({
      kind: 'container',
      seq: container.seq,
      items: container.items,
      running,
    })
  }

  return nodes
}

/** One entry of a turn-level reference list: raw citation path + whether the body footnotes cite it. */
export interface TurnRefEntry {
  raw: string
  cited: boolean
}

/**
 * Collect citation-provenance data from the session item stream (turns split by user
 * messages):
 * - allRaws: every path awaiting resolve (body footnote definitions ∪ tool-consulted
 *   paths), fed to useResolvedKnowledgePaths;
 * - footerBySeq: the "references" list of finished turns, keyed by the seq of the
 *   turn's last assistant message (the footer renders beneath it). Cited entries come
 *   first; tool-consulted paths dedup against cited paths by the anchor-stripped file
 *   path (D8 semantics: read direct + explicit shell_run command paths + subtask tool
 *   steps).
 */
export function collectCitationRefs(
  items: ChatItem[],
  running: boolean,
): { allRaws: string[]; footerBySeq: Map<number, TurnRefEntry[]> } {
  interface Seg {
    citedRaws: string[]
    toolRaws: string[]
    lastAssistantSeq: number | null
  }
  const segs: Seg[] = []
  let cur: Seg = { citedRaws: [], toolRaws: [], lastAssistantSeq: null }
  const pushUnique = (list: string[], v: string) => {
    if (!list.includes(v)) list.push(v)
  }
  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    if (i > 0 && item.kind === 'user') {
      segs.push(cur)
      cur = { citedRaws: [], toolRaws: [], lastAssistantSeq: null }
    }
    if (item.kind === 'step') {
      for (const p of extractKnowledgePaths(item.toolName, item.args)) {
        pushUnique(cur.toolRaws, p)
      }
    } else if (item.kind === 'subtask') {
      for (const s of item.steps) {
        for (const p of extractKnowledgePaths(s.toolName, s.args)) {
          pushUnique(cur.toolRaws, p)
        }
      }
    } else if (item.kind === 'assistant' && !isBackgroundNotice(item.text)) {
      cur.lastAssistantSeq = item.seq
      for (const d of parseCitationDefs(item.text)) {
        pushUnique(cur.citedRaws, d.raw)
      }
    }
  }
  segs.push(cur)

  const allRaws: string[] = []
  const footerBySeq = new Map<number, TurnRefEntry[]>()
  const basePath = (raw: string) => raw.split('#', 1)[0]
  segs.forEach((seg, idx) => {
    for (const raw of [...seg.citedRaws, ...seg.toolRaws]) {
      pushUnique(allRaws, raw)
    }
    // The last segment is still running: citation superscripts render live with the
    // message; the footer waits for the turn to end.
    const ended = idx < segs.length - 1 || !running
    if (!ended || seg.lastAssistantSeq === null) return
    const citedBases = new Set(seg.citedRaws.map(basePath))
    const entries: TurnRefEntry[] = [
      ...seg.citedRaws.map((raw) => ({ raw, cited: true })),
      ...seg.toolRaws
        .filter((raw) => !citedBases.has(basePath(raw)))
        .map((raw) => ({ raw, cited: false })),
    ]
    if (entries.length > 0) footerBySeq.set(seg.lastAssistantSeq, entries)
  })
  return { allRaws, footerBySeq }
}

/** First non-empty line, truncated to maxLen characters. */
export function truncateFirstLine(text: string, maxLen: number): string {
  const line =
    text
      .split('\n')
      .map((l) => l.trim())
      .find((l) => l.length > 0) ?? ''
  return line.length > maxLen ? `${line.slice(0, maxLen)}…` : line
}

/**
 * Single-line preview of tool arguments, shown inline after the tool name. Objects
 * flatten to `key: value` (string values unquoted for scannability), everything else
 * JSON-serialized; newlines squashed; truncated to maxLen with `…`.
 */
export function argsPreview(args: unknown, maxLen = 72): string {
  if (args == null) return ''
  let s: string
  if (typeof args === 'object' && !Array.isArray(args)) {
    s = Object.entries(args as Record<string, unknown>)
      .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join(', ')
  } else if (typeof args === 'string') {
    s = args
  } else {
    s = JSON.stringify(args)
  }
  s = s.replace(/\s+/g, ' ').trim()
  return s.length > maxLen ? `${s.slice(0, maxLen)}…` : s
}

/**
 * One entry of the expanded process timeline. thinking and tool calls merge into the
 * same timeline (R2); tool calls unify as `step` regardless of success / failure /
 * running (state derived from result, each expandable, successes not counted away);
 * intermediate assistant bodies and answered questions stand apart as "blocks" (R2.2).
 */
export type TimelineEntry =
  | { kind: 'thinking'; text: string }
  | { kind: 'step'; step: StepItem }
  | { kind: 'skill'; skill: string }
  | { kind: 'memory'; op: MemoryOp; name: string }
  | { kind: 'subtask'; item: SubtaskItem }
  | { kind: 'assistant'; text: string }
  | { kind: 'question'; item: QuestionItemView }

/** Labels for the four memory operations (shared by the session timeline and short status). */
export const MEMORY_OP_LABELS: Record<MemoryOp, string> = {
  write: 'Write memory',
  read: 'Read memory',
  search: 'Search memory',
  archive: 'Archive memory',
}

/**
 * Fold a container's visible process items into a unified timeline:
 * - tool calls kept one-by-one (visual compression is delegated to groupTimeline's
 *   activity-group folding);
 * - skill / memory / subtask / intermediate assistant / answered question merge in;
 * - thinking never enters the timeline: the thought process is not shown; dropped at
 *   the data layer.
 */
export function buildTimeline(items: ChatItem[]): TimelineEntry[] {
  const out: TimelineEntry[] = []
  for (const it of items) {
    switch (it.kind) {
      case 'step':
        out.push({ kind: 'step', step: it })
        break
      case 'skill':
        out.push({ kind: 'skill', skill: it.skill })
        break
      case 'memory':
        out.push({ kind: 'memory', op: it.op, name: it.name })
        break
      case 'subtask':
        out.push({ kind: 'subtask', item: it })
        break
      case 'assistant':
        out.push({ kind: 'assistant', text: it.text })
        break
      case 'question':
        out.push({ kind: 'question', item: it })
        break
      // thinking / user / todos / error / turn_end never enter the timeline
      // (buildNodes filtered them or the thought process is hidden); ignored.
    }
  }
  return out
}

/** Rows inside an activity group: every timeline process item except the "blocks" (assistant / question). */
export type ActivityRow = Exclude<
  TimelineEntry,
  { kind: 'assistant' } | { kind: 'question' }
>

/** Timeline grouping: bodies / follow-up questions are standalone "blocks"; consecutive
 * process items between them merge into one collapsible activity group. */
export type TimelineGroup =
  | { type: 'block'; entry: Extract<TimelineEntry, { kind: 'assistant' } | { kind: 'question' }> }
  | { type: 'activity'; rows: ActivityRow[] }

export function groupTimeline(entries: TimelineEntry[]): TimelineGroup[] {
  const groups: TimelineGroup[] = []
  for (const e of entries) {
    if (e.kind === 'assistant' || e.kind === 'question') {
      groups.push({ type: 'block', entry: e })
    } else {
      const last = groups[groups.length - 1]
      if (last?.type === 'activity') last.rows.push(e)
      else groups.push({ type: 'activity', rows: [e] })
    }
  }
  return groups
}

/** Collapsed-state summary of an activity group. */
export interface ActivitySummary {
  /** Main label: "N tool calls" (plus ", M subtasks" when present); with no tools falls
   * back to "Thinking" / "Activity log". */
  label: string
  /** Count of failed tools + subtasks; when >0 an "· N failed" suffix renders in danger color. */
  failed: number
  /** Name of the running tool / subtask agent (non-null while the group has in-flight activity). */
  runningTool: string | null
}

export function activitySummary(rows: ActivityRow[]): ActivitySummary {
  let tools = 0
  let subtasks = 0
  let failed = 0
  let hasThinking = false
  let runningTool: string | null = null
  for (const r of rows) {
    if (r.kind === 'step') {
      tools += 1
      if (r.step.result === null) runningTool = r.step.toolName
      else if (!r.step.result.success) failed += 1
    } else if (r.kind === 'subtask') {
      subtasks += 1
      if (r.item.status === 'running') runningTool = r.item.agentName
      else if (r.item.status === 'failed') failed += 1
    } else if (r.kind === 'thinking') {
      hasThinking = true
    }
  }
  const parts: string[] = []
  if (tools > 0) parts.push(`${tools} tool call${tools === 1 ? '' : 's'}`)
  if (subtasks > 0) parts.push(`${subtasks} subtask${subtasks === 1 ? '' : 's'}`)
  const label = parts.length > 0 ? parts.join(', ') : hasThinking ? 'Thinking' : 'Activity log'
  return { label, failed, runningTool }
}

/**
 * Status-bar text for the running collapsed state. Text only, no commands / arguments;
 * thinking / the thought process is excluded (requirement: thinking is never shown).
 * Priority:
 *   1) newest unfinished main tool: `Running · <toolName>`
 *   2) newest running subtask: `Subtask · <agentName> working`
 *   3) newest skill / memory / intermediate-assistant short status
 *   4) fallback `Running…`
 * (todos are surfaced separately outside the container and stay out of the status bar.)
 */
export function runningStatus(items: ChatItem[]): string {
  // 1) Newest unfinished main tool (subtask tool steps are not top-level items, so
  // naturally excluded).
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'step' && it.result === null) return `Running · ${it.toolName}`
  }
  // 2) Newest running subtask
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'subtask' && it.status === 'running')
      return `Subtask · ${it.agentName} working`
  }
  // 3) Newest skill / memory / intermediate-assistant short status (background notices
  // and thinking excluded).
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'skill') return `Skill · ${it.skill}`
    if (it.kind === 'memory') return `${MEMORY_OP_LABELS[it.op]} · ${it.name}`
    if (it.kind === 'assistant' && !isBackgroundNotice(it.text)) {
      const line = truncateFirstLine(it.text, 60)
      if (line) return line
    }
  }
  return 'Running…'
}
