import { useCallback, useEffect, useReducer, useRef } from 'react'
import { contentUrl, eventsUrl } from '../api/endpoints'
import { readSSE } from '../api/sse'
import type {
  MemoryOp,
  QuestionItem,
  SSEFrame,
  TodoItem,
  TurnStatus,
  WorkflowView,
} from '../api/types'
import {
  applyDelta,
  clearStreaming,
  resetCall,
  type StreamingState,
} from './streaming'

// ---- Render-item model: SSE events folded in order ----

/** An image shown in a user bubble. Real events carry a content hash (src is
 * the /content/{hash} URL); optimistic sends have no hash yet (src is the
 * local data URL) and are replaced when the real user_message arrives. */
export interface UserImage {
  hash: string | null
  src: string
}

export interface UserItem {
  kind: 'user'
  seq: number
  content: string
  /** Composer image attachments (absent for text-only messages). */
  images?: UserImage[]
}

export interface AssistantItem {
  kind: 'assistant'
  seq: number
  text: string
}

export interface ThinkingItem {
  kind: 'thinking'
  seq: number
  text: string
}

export interface StepItem {
  kind: 'step'
  seq: number
  callId: string
  toolName: string
  args: unknown
  /** null = still running */
  result: { success: boolean; summary: string; output: string } | null
}

export interface SkillItem {
  kind: 'skill'
  seq: number
  skill: string
}

export interface TodosItem {
  kind: 'todos'
  seq: number
  todos: TodoItem[]
}

/** Subtask card: spawn opens the card, tool events are grouped in by subtask_id, finished closes it. */
export interface SubtaskItem {
  kind: 'subtask'
  seq: number
  subtaskId: string
  agentName: string
  goal: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  summary: string
  /** The subtask's tool activity (reuses the StepItem shape for timeline rendering). */
  steps: StepItem[]
}

export interface MemoryOpItem {
  kind: 'memory'
  seq: number
  op: MemoryOp
  /** Operation target: memory name for write/read/archive, query string for search. */
  name: string
}

export interface QuestionItemView {
  kind: 'question'
  seq: number
  questionId: string
  reason?: string | null
  questions: QuestionItem[]
  answered: boolean
}

export interface ErrorItem {
  kind: 'error'
  seq: number
  message: string
}

/** Compaction divider: early history was folded into a summary (details on the Trace page). */
export interface CompactionItem {
  kind: 'compaction'
  seq: number
  /** Number of messages folded away. */
  replaced: number
}

export interface TurnEndItem {
  kind: 'turn_end'
  seq: number
  status: TurnStatus
}

export type ChatItem =
  | UserItem
  | AssistantItem
  | ThinkingItem
  | StepItem
  | SkillItem
  | TodosItem
  | MemoryOpItem
  | SubtaskItem
  | QuestionItemView
  | ErrorItem
  | TurnEndItem
  | CompactionItem

export interface ChatState {
  items: ChatItem[]
  running: boolean
  /** Workflow session view (full idempotent snapshot from workflow_update frames); always null for non-workflow sessions. */
  workflow: WorkflowView | null
  /** Whether the initial replay finished (for the skeleton state); set by the replay_done frame. */
  connected: boolean
  /** Whether the SSE socket is open (for the connection status dot). */
  sockOpen: boolean
  connectionError: string | null
  lastSeq: number
  /** Render keys for synthetic frames (seq null): monotonically decreasing negatives, never colliding with real seqs. */
  syntheticSeq: number
  /** Sequence number of the latest turn_finished, used to trigger a files-panel refresh. */
  turnEndCounter: number
  /** Live token-streaming preview: a single buffered call per session; null = no preview.
   * A delta is a transient projection (ADR token-streaming-projection) — never enters
   * items, never participates in dedup; cleared as soon as the durable assistant_text /
   * turn_finished / error arrives. A different call_id replaces the whole buffer
   * (naturally handling a retry's dropped half-stream). */
  streaming: StreamingState | null
  /** Title carried by the latest session_meta frame (task D): pushed when the
   * asynchronously LLM-generated title lands; the caller updates the current session +
   * sidebar list in place. null = none received for this session yet. Synthetic frames
   * are not replayed — after a refresh the sessions API already returns the new title,
   * so nothing needs persisting here. */
  metaTitle: string | null
}

const initialState: ChatState = {
  items: [],
  running: false,
  workflow: null,
  connected: false,
  sockOpen: false,
  connectionError: null,
  lastSeq: 0,
  syntheticSeq: -1,
  turnEndCounter: 0,
  streaming: null,
  metaTitle: null,
}

type Action =
  | { type: 'reset' }
  | { type: 'sock_open' }
  | { type: 'sock_closed' }
  | { type: 'conn_error'; message: string }
  | { type: 'frame'; frame: SSEFrame }
  | { type: 'question_answered_local'; questionId: string }
  /** imageDataUrls: local previews of the attachments riding this send. */
  | { type: 'optimistic_send'; content: string; imageDataUrls?: string[] }

/** Close pending steps on abnormal / terminal states: cancelled tools and drive-layer
 * failures (LLM errors etc.) never get a paired result; without closing they would
 * show "running" forever. */
function closePendingSteps(items: ChatItem[], summary: string): ChatItem[] {
  if (!items.some((it) => it.kind === 'step' && it.result === null)) return items
  return items.map((it) =>
    it.kind === 'step' && it.result === null
      ? { ...it, result: { success: false, summary, output: '' } }
      : it,
  )
}

/** Patch a subtask card in place by subtaskId (returns items unchanged when absent). */
function patchSubtask(
  items: ChatItem[],
  subtaskId: string,
  patch: (it: SubtaskItem) => SubtaskItem,
): ChatItem[] {
  const idx = items.findLastIndex(
    (it) => it.kind === 'subtask' && it.subtaskId === subtaskId,
  )
  if (idx < 0) return items
  return [
    ...items.slice(0, idx),
    patch(items[idx] as SubtaskItem),
    ...items.slice(idx + 1),
  ]
}

/** Close still-running subtask cards on cancel: the subtask-finish events cascaded by
 * cancel are synthetic frames (not replayed); after a refresh only the parent stream's
 * turn_finished{cancelled} can close them. */
function closeRunningSubtasks(items: ChatItem[]): ChatItem[] {
  if (!items.some((it) => it.kind === 'subtask' && it.status === 'running')) {
    return items
  }
  return items.map((it) =>
    it.kind === 'subtask' && it.status === 'running'
      ? {
          ...it,
          status: 'cancelled' as const,
          steps: it.steps.map((s) =>
            s.result === null
              ? { ...s, result: { success: false, summary: 'Stopped', output: '' } }
              : s,
          ),
        }
      : it,
  )
}

function foldFrame(state: ChatState, frame: SSEFrame): ChatState {
  // Only frames with a seq participate in replay dedup; synthetic events (seq null) are handled directly.
  if (frame.seq !== null && frame.seq <= state.lastSeq) return state
  const ev = frame.event
  // Token streaming: a transient projection — never enters items, never touches lastSeq;
  // accumulated by (call_id, index), a different call_id replaces the whole buffer —
  // cleared when the durable assistant_text/turn_finished/error arrives.
  if (ev.type === 'delta') {
    return { ...state, streaming: applyDelta(state.streaming, ev.data) }
  }
  // LLM retry: before the same call_id re-streams, clear that call's half-received
  // buffer to avoid garbled concatenation. Does not enter items (no UI bar); only
  // updates streaming; seq is recorded as usual.
  if (ev.type === 'llm_retry') {
    return {
      ...state,
      streaming: resetCall(state.streaming, ev.data.call_id),
      lastSeq: frame.seq ?? state.lastSeq,
      syntheticSeq: frame.seq === null ? state.syntheticSeq - 1 : state.syntheticSeq,
    }
  }
  if (ev.type === 'replay_done') {
    return { ...state, connected: true }
  }
  // session_meta (task D): synthetic frame pushed when the async LLM title lands
  // (seq=null, not replayed). Only exposes metaTitle for the caller to update the
  // session / sidebar title in place; never enters items, never touches lastSeq.
  if (ev.type === 'session_meta') {
    return { ...state, metaTitle: ev.data.title }
  }
  // workflow_update is a full idempotent snapshot (seq=null): overwrites the tab-bar view, never enters items.
  if (ev.type === 'workflow_update') {
    return { ...state, workflow: ev.data }
  }
  const seq = frame.seq ?? state.syntheticSeq
  let items = state.items
  let running = state.running
  let turnEndCounter = state.turnEndCounter
  // Durable truth landing / terminal state / error: clear the streaming preview (the
  // preview bubble is seamlessly replaced by the real content). reset also clears
  // streaming (case 'reset' returns initialState).
  let streaming = state.streaming

  switch (ev.type) {
    case 'user_message': {
      // Attached images arrive as content hashes; the bytes are fetched from
      // the content endpoint (the local data-URL preview of an optimistic
      // send is replaced along with its item below).
      const images: UserImage[] | undefined = ev.data.images?.map((img) => ({
        hash: img.hash,
        src: contentUrl(img.hash),
      }))
      const real: UserItem = images
        ? { kind: 'user', seq, content: ev.data.content, images }
        : { kind: 'user', seq, content: ev.data.content }
      // Dedup: optimistic_send already rendered a synthetic user item (seq < 0); when
      // the real user_message arrives it replaces that item (with the real seq) instead
      // of appending. Checking only the last item is not enough — after a reset the SSE
      // replay may arrive before optimisticSend.
      const synthIdx = items.findLastIndex(
        (it) => it.kind === 'user' && it.seq < 0 && it.content === ev.data.content,
      )
      if (synthIdx >= 0) {
        items = [...items.slice(0, synthIdx), real, ...items.slice(synthIdx + 1)]
        break
      }
      // Fallback: also skip when the last item is a real user message with identical
      // content (guards against duplicate SSE reconnect replays).
      const last = items[items.length - 1]
      if (last?.kind === 'user' && last.content === ev.data.content) break
      items = [...items, real]
      break
    }
    case 'assistant_text':
      items = [...items, { kind: 'assistant', seq, text: ev.data.text }]
      streaming = clearStreaming()
      break
    case 'thinking':
      items = [...items, { kind: 'thinking', seq, text: ev.data.text }]
      // The thinking-delta preview must clear too: once durable thinking lands the
      // preview bubble must not double-render.
      streaming = clearStreaming()
      break
    case 'tool_call': {
      const step: StepItem = {
        kind: 'step',
        seq,
        callId: ev.data.call_id,
        toolName: ev.data.tool_name,
        args: ev.data.arguments,
        result: null,
      }
      const subId = ev.data.subtask_id
      if (subId) {
        // A subtask's tool activity is grouped into its card (dropped when the card
        // does not exist — e.g. a foreground spawn outside the known set).
        items = patchSubtask(items, subId, (it) => ({
          ...it,
          steps: [...it.steps, step],
        }))
      } else {
        items = [...items, step]
      }
      break
    }
    case 'tool_result': {
      const result = {
        success: ev.data.success,
        summary: ev.data.summary,
        output: ev.data.output,
      }
      const subId = ev.data.subtask_id
      if (subId) {
        items = patchSubtask(items, subId, (it) => {
          const si = it.steps.findLastIndex((s) => s.callId === ev.data.call_id)
          if (si < 0) return it
          const steps = [...it.steps]
          steps[si] = { ...steps[si], result }
          return { ...it, steps }
        })
        break
      }
      const idx = items.findLastIndex(
        (it) => it.kind === 'step' && it.callId === ev.data.call_id,
      )
      if (idx >= 0) {
        const step = items[idx] as StepItem
        items = [
          ...items.slice(0, idx),
          { ...step, result },
          ...items.slice(idx + 1),
        ]
      }
      break
    }
    case 'skill_activated':
      items = [...items, { kind: 'skill', seq, skill: ev.data.skill }]
      break
    case 'todo_update': {
      // set_todos replaces wholesale: when this turn (after the last user message)
      // already has a checklist, update it in place so a turn renders only one todo card.
      const lastUser = items.findLastIndex((it) => it.kind === 'user')
      const idx = items.findLastIndex(
        (it, i) => it.kind === 'todos' && i > lastUser,
      )
      if (idx >= 0) {
        const cur = items[idx] as TodosItem
        items = [
          ...items.slice(0, idx),
          { ...cur, todos: ev.data.todos },
          ...items.slice(idx + 1),
        ]
      } else {
        items = [...items, { kind: 'todos', seq, todos: ev.data.todos }]
      }
      break
    }
    case 'memory_op':
      items = [
        ...items,
        { kind: 'memory', seq, op: ev.data.op, name: ev.data.name },
      ]
      break
    case 'compaction':
      items = [
        ...items,
        { kind: 'compaction', seq, replaced: ev.data.replaced_count },
      ]
      break
    case 'subtask_started':
      items = [
        ...items,
        {
          kind: 'subtask',
          seq,
          subtaskId: ev.data.subtask_id,
          agentName: ev.data.agent_name,
          goal: ev.data.goal,
          status: 'running',
          summary: '',
          steps: [],
        },
      ]
      break
    case 'subtask_finished':
      items = patchSubtask(items, ev.data.subtask_id, (it) => ({
        ...it,
        status: ev.data.status,
        summary: ev.data.summary,
        steps: it.steps.map((s) =>
          s.result === null
            ? { ...s, result: { success: false, summary: 'Finished', output: '' } }
            : s,
        ),
      }))
      break
    case 'question':
      items = [
        ...items,
        {
          kind: 'question',
          seq,
          questionId: ev.data.question_id,
          reason: ev.data.reason,
          questions: ev.data.questions,
          answered: false,
        },
      ]
      // A follow-up question arriving = the LLM is done and waiting for user input; clear the preview.
      streaming = clearStreaming()
      break
    case 'question_answered': {
      items = items.map((it) =>
        it.kind === 'question' && it.questionId === ev.data.question_id
          ? { ...it, answered: true }
          : it,
      )
      break
    }
    case 'turn_started':
      running = true
      break
    case 'turn_finished': {
      running = false
      turnEndCounter += 1
      // Turn terminal state: clear every preview (success/failure/cancel — the durable events have landed).
      streaming = clearStreaming()
      // Only show the terminal marker when the turn did not end in a normal wait-for-input.
      if (ev.data.status === 'cancelled' || ev.data.status === 'failed') {
        items = closePendingSteps(
          items,
          ev.data.status === 'cancelled'
            ? 'Stopped'
            : 'Execution interrupted; no result received',
        )
        if (ev.data.status === 'cancelled') {
          // Cancel cascades to background subtasks; the synthetic closing frames are
          // not replayed, so this is the fallback.
          items = closeRunningSubtasks(items)
        }
        items = [...items, { kind: 'turn_end', seq, status: ev.data.status }]
      }
      break
    }
    case 'error':
      // An error may arrive without a turn_finished (an answer drive failure only
      // pushes error; or the turn_finished frame was lost on the live stream and only
      // error made it). Close everything and unlock running here too — running must
      // not depend solely on turn_finished, or one lost frame would leave the UI
      // stuck on "running" forever.
      running = false
      items = closePendingSteps(items, 'Execution interrupted; no result received')
      items = [...items, { kind: 'error', seq, message: ev.data.message }]
      streaming = clearStreaming()
      break
  }

  return {
    ...state,
    items,
    running,
    turnEndCounter,
    streaming,
    lastSeq: frame.seq ?? state.lastSeq,
    syntheticSeq: frame.seq === null ? state.syntheticSeq - 1 : state.syntheticSeq,
  }
}

function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case 'reset':
      return initialState
    case 'sock_open':
      return { ...state, sockOpen: true, connectionError: null }
    case 'sock_closed':
      return { ...state, sockOpen: false }
    case 'conn_error':
      return { ...state, sockOpen: false, connectionError: action.message }
    case 'frame':
      return foldFrame(state, action.frame)
    case 'question_answered_local':
      return {
        ...state,
        items: state.items.map((it) =>
          it.kind === 'question' && it.questionId === action.questionId
            ? { ...it, answered: true }
            : it,
        ),
      }
    case 'optimistic_send':
      // Render immediately after sending the first message: while seed_start blocks on
      // sandbox cold start, the user_message event only emits once the container is
      // ready — show it optimistically here + mark running, and let foldFrame replace
      // it when the real event arrives.
      // Dedup guard: if items already holds a real user message (seq > 0) with the same
      // content, the SSE replay beat optimisticSend; do not append again.
      if (
        state.items.some(
          (it) =>
            it.kind === 'user' && it.seq > 0 && it.content === action.content,
        )
      ) {
        return state
      }
      return {
        ...state,
        items: [
          ...state.items,
          {
            kind: 'user',
            seq: state.syntheticSeq - 1,
            content: action.content,
            ...(action.imageDataUrls && action.imageDataUrls.length > 0
              ? {
                  images: action.imageDataUrls.map((src) => ({
                    hash: null,
                    src,
                  })),
                }
              : {}),
          },
        ],
        running: true,
        syntheticSeq: state.syntheticSeq - 1,
      }
  }
}

/** Subscribe to a session's event stream: replay + live + auto-reconnect.

 * Workflow sessions (ADR-0012): pass taskId to subscribe to a single node task's
 * stream (per-tab); switching tabs = a new taskId → reset and reconnect; untagged
 * session-level frames (workflow_update etc.) reach every tab. */
export function useChat(sessionId: string | null, taskId?: string | null) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const lastSeqRef = useRef(0)
  lastSeqRef.current = state.lastSeq

  useEffect(() => {
    dispatch({ type: 'reset' })
    lastSeqRef.current = 0
    if (!sessionId) return

    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempt = 0

    const connect = () => {
      readSSE(
        eventsUrl(sessionId, lastSeqRef.current, taskId),
        controller.signal,
        (frame) => {
          dispatch({ type: 'frame', frame })
        },
        () => {
          attempt = 0
          dispatch({ type: 'sock_open' })
        },
      )
        .then(() => {
          if (controller.signal.aborted) return
          dispatch({ type: 'sock_closed' })
          scheduleRetry()
        })
        .catch((e: unknown) => {
          if (controller.signal.aborted) return
          dispatch({
            type: 'conn_error',
            message: e instanceof Error ? e.message : 'Connection lost',
          })
          scheduleRetry()
        })
    }

    const scheduleRetry = () => {
      if (controller.signal.aborted) return
      attempt += 1
      const delay = Math.min(1000 * 2 ** Math.min(attempt - 1, 3), 8000)
      retryTimer = window.setTimeout(connect, delay)
    }

    connect()
    return () => {
      controller.abort()
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [sessionId, taskId])

  const markAnswered = useCallback((questionId: string) => {
    dispatch({ type: 'question_answered_local', questionId })
  }, [])

  const optimisticSend = useCallback(
    (content: string, imageDataUrls?: string[]) => {
      dispatch({ type: 'optimistic_send', content, imageDataUrls })
    },
    [],
  )

  return { ...state, markAnswered, optimisticSend }
}

/** The follow-up question currently awaiting an answer (the last unanswered question). */
export function pendingQuestion(items: ChatItem[]): QuestionItemView | null {
  const last = items.findLast(
    (it) => it.kind === 'question' && !it.answered,
  )
  return (last as QuestionItemView | undefined) ?? null
}
