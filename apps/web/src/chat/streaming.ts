/**
 * Token streaming buffer (ADR token-streaming-projection; mirrors the pure-function
 * design of the earlier SPA's domain/streaming.js).
 *
 * A delta is a transient projection: it only exists in live SSE frames while a turn
 * is in progress; replay (since_seq) never contains it. The durable truth is the
 * assistant_text (full text) that follows, at which point the buffer is cleared
 * wholesale.
 *
 * state shape: a single buffered call per session.
 *
 *   { callId: string, blocks: Map<index, { kind: 'text'|'thinking', text: string }> }
 *
 * - applyDelta: accumulate text by (call_id, index); a different call_id replaces the
 *   whole state (naturally discarding a half-stream dropped by a retry).
 * - clear: after the turn ends (assistant_text / turn_finished / error) the whole
 *   buffer is discarded.
 * - renderBlocks: {kind, text}[] sorted by index for UI rendering; returns null when
 *   nothing is visible.
 */

export interface StreamingBlock {
  kind: 'text' | 'thinking'
  text: string
  index: number
}

export interface StreamingState {
  callId: string
  blocks: Map<number, { kind: 'text' | 'thinking'; text: string }>
}

export type DeltaPayload = {
  call_id: string
  kind: 'text' | 'thinking'
  text: string
  index: number
}

function isValidDelta(d: Partial<DeltaPayload> | null | undefined): d is DeltaPayload {
  return (
    !!d &&
    typeof d.call_id === 'string' &&
    d.call_id !== '' &&
    (d.kind === 'text' || d.kind === 'thinking') &&
    typeof d.text === 'string' &&
    typeof d.index === 'number' &&
    Number.isFinite(d.index)
  )
}

export function createStreamingState(): StreamingState {
  return { callId: '', blocks: new Map() }
}

/** Returning the same state reference means no change (for React shallow comparison / useReducer). */
export function applyDelta(
  state: StreamingState | null,
  delta: unknown,
): StreamingState | null {
  const d = delta as Partial<DeltaPayload> | null | undefined
  if (!isValidDelta(d)) return state
  const sameCall = state !== null && state.callId === d.call_id
  const blocks = new Map(sameCall ? state.blocks : undefined)
  const existing = blocks.get(d.index)
  const text =
    existing && existing.kind === d.kind ? existing.text + d.text : d.text
  blocks.set(d.index, { kind: d.kind, text })
  return { callId: d.call_id, blocks }
}

/** Clear the buffer (when the durable message lands, the turn ends, the session switches, or on reconnect). */
export function clearStreaming(): null {
  return null
}

/**
 * Reset the buffer by call_id: a transient LLM failure retries re-streaming under
 * the same call_id, so that call's half-received deltas must be cleared or they
 * would concatenate with the new stream into garbage. A mismatched callId (the
 * buffer already belongs to another call) is a no-op — matching the earlier
 * streaming.js resetCall "no-op on callId mismatch" semantics, defensively safe.
 * A null callId clears unconditionally.
 */
export function resetCall(
  state: StreamingState | null,
  callId: string | null,
): StreamingState | null {
  if (!state) return null
  if (callId != null && state.callId !== callId) return state
  return null
}

/** For rendering: block list sorted by index; empty-text blocks dropped; null when nothing is visible. */
export function renderBlocks(
  state: StreamingState | null,
): StreamingBlock[] | null {
  if (!state) return null
  const out: StreamingBlock[] = []
  for (const [index, b] of state.blocks) {
    if (b.text) out.push({ kind: b.kind, text: b.text, index })
  }
  if (!out.length) return null
  out.sort((a, b) => a.index - b.index)
  return out
}
