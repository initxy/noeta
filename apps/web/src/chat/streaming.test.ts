/**
 * Streaming-buffer pure-function unit tests: accumulation, call_id replacement,
 * clearing, invalid-frame tolerance. Mirrors the case design of the earlier SPA's
 * streaming.js.
 */
import { describe, expect, it } from 'vitest'
import {
  applyDelta,
  clearStreaming,
  createStreamingState,
  renderBlocks,
  resetCall,
} from './streaming'

const d = (call_id: string, kind: 'text' | 'thinking', text: string, index: number) => ({
  call_id,
  kind,
  text,
  index,
})

describe('streaming buffer', () => {
  it('empty state renders null', () => {
    expect(renderBlocks(null)).toBeNull()
    expect(renderBlocks(createStreamingState())).toBeNull()
  })

  it('the first delta establishes the buffer', () => {
    const s = applyDelta(null, d('c1', 'text', 'Hello', 0))
    expect(renderBlocks(s)).toEqual([
      { kind: 'text', text: 'Hello', index: 0 },
    ])
  })

  it('same call_id + same index accumulates text', () => {
    let s = applyDelta(null, d('c1', 'text', 'Hello', 0))
    s = applyDelta(s, d('c1', 'text', ', world', 0))
    s = applyDelta(s, d('c1', 'text', '.', 0))
    expect(renderBlocks(s)).toEqual([
      { kind: 'text', text: 'Hello, world.', index: 0 },
    ])
  })

  it('different indexes sort in order; kinds render separately', () => {
    let s = applyDelta(null, d('c1', 'thinking', 'thinking...', 0))
    s = applyDelta(s, d('c1', 'text', 'body', 1))
    expect(renderBlocks(s)).toEqual([
      { kind: 'thinking', text: 'thinking...', index: 0 },
      { kind: 'text', text: 'body', index: 1 },
    ])
  })

  it('a different call_id replaces the old buffer wholesale (retry scenario)', () => {
    let s = applyDelta(null, d('c-old', 'text', 'half-stream', 0))
    s = applyDelta(s, d('c-new', 'text', 're-stream', 0))
    // The old call's half-received content must not survive.
    expect(renderBlocks(s)).toEqual([
      { kind: 'text', text: 're-stream', index: 0 },
    ])
  })

  it('a kind flip at the same index replaces that block wholesale (defensive)', () => {
    let s = applyDelta(null, d('c1', 'thinking', 'thought', 0))
    s = applyDelta(s, d('c1', 'text', 'answer', 0))
    expect(renderBlocks(s)).toEqual([
      { kind: 'text', text: 'answer', index: 0 },
    ])
  })

  it('clearStreaming returns null (cleared after the durable message lands)', () => {
    let s = applyDelta(null, d('c1', 'text', 'abc', 0))
    s = clearStreaming()
    expect(s).toBeNull()
    expect(renderBlocks(s)).toBeNull()
  })

  it('invalid deltas leave the state untouched (forward compatibility + malformed-frame safety)', () => {
    const s0 = null
    // missing fields
    expect(applyDelta(s0, null)).toBe(s0)
    expect(applyDelta(s0, {})).toBe(s0)
    expect(applyDelta(s0, { call_id: '', kind: 'text', text: 'x', index: 0 })).toBe(s0)
    expect(applyDelta(s0, { call_id: 'c', kind: 'bad', text: 'x', index: 0 })).toBe(s0)
    expect(applyDelta(s0, { call_id: 'c', kind: 'text', text: 123 as any, index: 0 })).toBe(s0)
    expect(applyDelta(s0, { call_id: 'c', kind: 'text', text: 'x', index: NaN })).toBe(s0)
  })

  it('empty-text blocks do not render (the call has produced no visible bytes yet)', () => {
    const s = applyDelta(null, d('c1', 'text', '', 0))
    expect(renderBlocks(s)).toBeNull()
  })

  it('resetCall with a matching call_id clears the buffer (retry: same call_id re-streams)', () => {
    let s = applyDelta(null, d('c1', 'text', 'half-stream', 0))
    expect(renderBlocks(s)).not.toBeNull()
    s = resetCall(s, 'c1')
    expect(s).toBeNull()
    expect(renderBlocks(s)).toBeNull()
  })

  it('resetCall with a mismatched call_id is a no-op (the buffer belongs to another call)', () => {
    const s = applyDelta(null, d('c1', 'text', 'abc', 0))
    const after = resetCall(s, 'c-other')
    // Same reference = no change.
    expect(after).toBe(s)
    expect(renderBlocks(after)).toEqual([
      { kind: 'text', text: 'abc', index: 0 },
    ])
  })

  it('resetCall with a null call_id clears unconditionally', () => {
    let s = applyDelta(null, d('c1', 'text', 'abc', 0))
    s = resetCall(s, null)
    expect(s).toBeNull()
  })

  it('resetCall on an empty state is a no-op', () => {
    expect(resetCall(null, 'c1')).toBeNull()
  })
})
