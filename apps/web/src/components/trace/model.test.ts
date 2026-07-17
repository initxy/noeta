import { describe, expect, it } from 'vitest'
import type { RawEnvelope } from '../../api/types'
import { collectCompactions } from './model'

function ev(
  type: string,
  seq: number,
  payload: unknown,
  taskId = 'task-main',
): RawEnvelope {
  return {
    id: `e${seq}`,
    task_id: taskId,
    seq,
    type,
    schema_version: 1,
    occurred_at: 1000 + seq,
    actor: 'engine',
    trace_id: 'tr',
    correlation_id: 'co',
    causation_id: null,
    payload,
    origin: 'engine',
  }
}

describe('collectCompactions — pairs Requested/Compacted into one compaction', () => {
  it('adjacent pair: reason comes from Requested, replaced_count from Compacted', () => {
    const out = collectCompactions([
      ev('CompactionRequested', 743, { estimated_tokens: 37484, reason: 'proactive' }),
      ev('Compacted', 744, { replaced_count: 103 }),
    ])
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({
      taskId: 'task-main',
      seq: 743,
      compactedSeq: 744,
      kind: 'proactive',
      estimatedTokens: 37484,
      replacedCount: 103,
    })
  })

  it('an orphan Compacted (no preceding request) becomes its own card with kind unknown', () => {
    const out = collectCompactions([ev('Compacted', 10, { replaced_count: 5 })])
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({
      seq: 10,
      compactedSeq: 10,
      kind: 'unknown',
      replacedCount: 5,
    })
  })

  it('a Requested that never lands (escalation interrupt) stays as a card with compactedSeq=null', () => {
    const out = collectCompactions([
      ev('CompactionRequested', 7, { estimated_tokens: 100, reason: 'passive' }),
    ])
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ seq: 7, compactedSeq: null, kind: 'passive' })
  })

  it('no cross-stream pairing: a subagent Compacted does not claim the main Requested', () => {
    const out = collectCompactions([
      ev('CompactionRequested', 3, { reason: 'proactive' }, 'task-main'),
      ev('Compacted', 3, { replaced_count: 9 }, 'task-sub'),
      ev('Compacted', 4, { replaced_count: 40 }, 'task-main'),
    ])
    expect(out).toHaveLength(2)
    const main = out.find((c) => c.taskId === 'task-main')
    const sub = out.find((c) => c.taskId === 'task-sub')
    expect(main).toMatchObject({ seq: 3, compactedSeq: 4, replacedCount: 40, kind: 'proactive' })
    expect(sub).toMatchObject({ seq: 3, compactedSeq: 3, kind: 'unknown', replacedCount: 9 })
  })

  it('multiple compactions in one stream sort by occurrence time and pair independently', () => {
    const out = collectCompactions([
      ev('CompactionRequested', 100, { reason: 'proactive' }),
      ev('Compacted', 101, { replaced_count: 50 }),
      ev('CompactionRequested', 300, { reason: 'passive' }),
      ev('Compacted', 301, { replaced_count: 20 }),
    ])
    expect(out.map((c) => [c.seq, c.compactedSeq, c.kind])).toEqual([
      [100, 101, 'proactive'],
      [300, 301, 'passive'],
    ])
  })
})
