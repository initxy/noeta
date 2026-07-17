import { describe, it, expect } from 'vitest'
import {
  activitySummary,
  argsPreview,
  buildNodes,
  buildTimeline,
  groupTimeline,
  runningStatus,
  type ActivityRow,
} from './conversationNodes'
import { type ChatItem } from '../chat/useChat'
import type { TodoItem } from '../api/types'

function thinking(seq: number, text: string): ChatItem {
  return { kind: 'thinking', seq, text }
}
function step(seq: number, toolName: string, done = true): ChatItem {
  return {
    kind: 'step',
    seq,
    callId: `c${seq}`,
    toolName,
    args: {},
    result: done
      ? { success: true, summary: 'ok', output: '' }
      : null,
  }
}
function assistant(seq: number, text: string): ChatItem {
  return { kind: 'assistant', seq, text }
}
function user(seq: number, content: string): ChatItem {
  return { kind: 'user', seq, content }
}
function compaction(seq: number, replaced: number): ChatItem {
  return { kind: 'compaction', seq, replaced }
}

describe('buildNodes', () => {
  it('an intermediate assistant folds based on visible process events (step); thinking neither participates nor enters the container', () => {
    // assistant(0) precedes the visible process step(2) → folded; assistant(3)
    // follows it → final result, stays in the main flow. thinking(1) does not
    // enter the container and does not affect "which is the last visible
    // process event".
    const items: ChatItem[] = [
      assistant(0, 'intermediate statement'),
      thinking(1, 't'),
      step(2, 'read'),
      assistant(3, 'final result'),
    ]
    const nodes = buildNodes(items, false)

    expect(nodes).toHaveLength(2)
    const n0 = nodes[0]
    expect(n0.kind).toBe('container')
    if (n0.kind === 'container') {
      expect(n0.items.map((i) => i.kind)).toEqual(['assistant', 'step'])
    }
    const n1 = nodes[1]
    expect(n1.kind).toBe('item')
    if (n1.kind === 'item') expect(n1.item.kind).toBe('assistant')
  })

  it('a trailing container of a running stream is marked running (no final assistant closes it)', () => {
    const items: ChatItem[] = [user(0, 'question'), thinking(1, 't'), step(2, 'read', false)]
    const nodes = buildNodes(items, true)

    expect(nodes).toHaveLength(2)
    const n0 = nodes[0]
    expect(n0.kind).toBe('item')
    if (n0.kind === 'item') expect(n0.item.kind).toBe('user')
    const n1 = nodes[1]
    expect(n1.kind).toBe('container')
    if (n1.kind === 'container') expect(n1.running).toBe(true)
  })

  it('single turn: user + process + final assistant', () => {
    const items: ChatItem[] = [
      user(0, 'question'),
      thinking(1, 't'),
      step(2, 'read'),
      assistant(3, 'answer'),
    ]
    const nodes = buildNodes(items, false)

    expect(nodes).toHaveLength(3)
    const n0 = nodes[0]
    expect(n0.kind).toBe('item')
    if (n0.kind === 'item') expect(n0.item.kind).toBe('user')
    expect(nodes[1].kind).toBe('container')
    const n2 = nodes[2]
    expect(n2.kind).toBe('item')
    if (n2.kind === 'item') expect(n2.item.kind).toBe('assistant')
  })

  it('the compaction divider stays in the main flow; landing mid-process cuts the container', () => {
    // Before and after compaction are two contexts anyway: step(1) and step(3)
    // belong to two containers with the compaction(2) main-flow divider between.
    const items: ChatItem[] = [
      user(0, 'question'),
      step(1, 'read'),
      compaction(2, 103),
      step(3, 'grep'),
      assistant(4, 'answer'),
    ]
    const nodes = buildNodes(items, false)
    expect(nodes.map((n) => n.kind)).toEqual([
      'item', 'container', 'item', 'container', 'item',
    ])
    const divider = nodes[2]
    if (divider.kind === 'item' && divider.item.kind === 'compaction') {
      expect(divider.item.replaced).toBe(103)
    } else {
      throw new Error('expected compaction item in main flow')
    }
  })
})

describe('buildNodes — thinking never produces an empty process container', () => {
  it('a pure-thinking turn yields no nodes (no empty container)', () => {
    expect(buildNodes([thinking(0, 't')], false)).toEqual([])
  })

  it('user + thinking + final assistant: no container, only user and the final assistant remain', () => {
    const items: ChatItem[] = [user(0, 'question'), thinking(1, 't'), assistant(2, 'answer')]
    const nodes = buildNodes(items, false)
    expect(nodes.map((n) => n.kind)).toEqual(['item', 'item'])
    const n0 = nodes[0]
    if (n0.kind === 'item') expect(n0.item.kind).toBe('user')
    const n1 = nodes[1]
    if (n1.kind === 'item' && n1.item.kind === 'assistant') {
      expect(n1.item.text).toBe('answer')
    }
  })

  it('running with only user + thinking: no container', () => {
    const nodes = buildNodes([user(0, 'question'), thinking(1, 't')], true)
    expect(nodes.map((n) => n.kind)).toEqual(['item'])
  })

  it('thinking + step: still one container, thinking absent from container.items', () => {
    const nodes = buildNodes([thinking(0, 't'), step(1, 'read', false)], true)
    expect(nodes.map((n) => n.kind)).toEqual(['container'])
    const c = nodes[0]
    if (c.kind === 'container') {
      expect(c.running).toBe(true)
      expect(c.items.map((i) => i.kind)).toEqual(['step'])
    }
  })
})

function failedStep(seq: number, toolName: string): ChatItem {
  return {
    kind: 'step',
    seq,
    callId: `c${seq}`,
    toolName,
    args: {},
    result: { success: false, summary: 'something went wrong', output: '' },
  }
}
function todos(seq: number, list: TodoItem[]): ChatItem {
  return { kind: 'todos', seq, todos: list }
}
function skill(seq: number, name: string): ChatItem {
  return { kind: 'skill', seq, skill: name }
}
function runningSubtask(seq: number, agentName: string): ChatItem {
  return {
    kind: 'subtask',
    seq,
    subtaskId: `s${seq}`,
    agentName,
    goal: 'g',
    status: 'running',
    summary: '',
    steps: [],
  }
}

describe('buildTimeline — thinking stays out of the timeline; tools are kept one-by-one', () => {
  it('successful and failed tools are all kept one-by-one (expandable); thinking is dropped', () => {
    const items: ChatItem[] = [
      thinking(0, 't0'),
      thinking(1, 't1'),
      step(2, 'read'),
      step(3, 'read'),
      failedStep(4, 'exec'),
      step(5, 'write'),
    ]
    const entries = buildTimeline(items)
    // thinking stays out of the timeline; only the 4 tool steps remain
    expect(entries.map((e) => e.kind)).toEqual(['step', 'step', 'step', 'step'])
    const failed = entries[2]
    if (failed.kind === 'step') expect(failed.step.result?.success).toBe(false)
  })

  it('an intermediate assistant body joins the timeline as a standalone block (thinking does not)', () => {
    const items: ChatItem[] = [thinking(0, 't'), assistant(1, 'intermediate statement'), step(2, 'read')]
    const entries = buildTimeline(items)
    expect(entries.map((e) => e.kind)).toEqual(['assistant', 'step'])
  })
})

describe('groupTimeline / activitySummary — process between bodies folds into activity groups', () => {
  it('a body block cuts the activity group; consecutive process entries merge into one', () => {
    const items: ChatItem[] = [
      thinking(0, 't0'),
      step(1, 'read'),
      assistant(2, 'intermediate statement'),
      step(3, 'write'),
      failedStep(4, 'exec'),
    ]
    const groups = groupTimeline(buildTimeline(items))
    expect(groups.map((g) => g.type)).toEqual(['activity', 'block', 'activity'])
    const g0 = groups[0]
    if (g0.type === 'activity') {
      // thinking stays out of the timeline; the first activity group only keeps the step
      expect(g0.rows.map((r) => r.kind)).toEqual(['step'])
    }
    const g2 = groups[2]
    if (g2.type === 'activity') expect(g2.rows).toHaveLength(2)
  })

  it('summary: tool count + failure count, successes not marked individually', () => {
    const groups = groupTimeline(
      buildTimeline([step(0, 'read'), step(1, 'write'), failedStep(2, 'exec')]),
    )
    const g = groups[0]
    expect(g.type).toBe('activity')
    if (g.type === 'activity') {
      const s = activitySummary(g.rows)
      expect(s.label).toBe('3 tool calls')
      expect(s.failed).toBe(1)
      expect(s.runningTool).toBeNull()
    }
  })

  it('summary: a running tool surfaces its name; a pure-thinking group is labeled Thinking', () => {
    const runningRows = groupTimeline(buildTimeline([step(0, 'exec', false)]))
    if (runningRows[0].type === 'activity') {
      expect(activitySummary(runningRows[0].rows).runningTool).toBe('exec')
    }
    const thinkingOnly: ActivityRow[] = [{ kind: 'thinking', text: 't' }]
    expect(activitySummary(thinkingOnly).label).toBe('Thinking')
  })
})

describe('runningStatus — main tool > subtask > skill/memory/assistant; thinking never participates', () => {
  it('a running main tool takes priority, ignoring thinking', () => {
    const items: ChatItem[] = [thinking(0, 'first line of thought\nsecond line'), step(1, 'read', false)]
    expect(runningStatus(items)).toBe('Running · read')
  })

  it('with only a running tool, shows the tool name without command/arguments', () => {
    const items: ChatItem[] = [step(0, 'shell_run', false)]
    expect(runningStatus(items)).toBe('Running · shell_run')
  })

  it('without a running main tool, surfaces the running subtask', () => {
    const items: ChatItem[] = [step(0, 'read'), runningSubtask(1, 'explorer')]
    expect(runningStatus(items)).toBe('Subtask · explorer working')
  })

  it('without tools/subtasks, falls back to the latest skill short status', () => {
    const items: ChatItem[] = [skill(0, 'tracking-design')]
    expect(runningStatus(items)).toBe('Skill · tracking-design')
  })

  it('the four memory operations each have a labeled short status (search targets the query string)', () => {
    const mem = (op: 'write' | 'read' | 'search' | 'archive', name: string) =>
      [{ kind: 'memory', seq: 0, op, name } as ChatItem]
    expect(runningStatus(mem('write', 'user-prefs'))).toBe('Write memory · user-prefs')
    expect(runningStatus(mem('read', 'user-prefs'))).toBe('Read memory · user-prefs')
    expect(runningStatus(mem('search', 'rate limiting'))).toBe('Search memory · rate limiting')
    expect(runningStatus(mem('archive', 'stale-note'))).toBe('Archive memory · stale-note')
  })

  it('neither thinking nor background-subtask notices feed the status bar; falls back', () => {
    const items: ChatItem[] = [
      thinking(0, 'thought'),
      assistant(1, '[subagent] Result from explorer (background task task-x): conclusion'),
    ]
    expect(runningStatus(items)).toBe('Running…')
  })
})

describe('buildNodes — todos are transparent and never cut the process container', () => {
  it('todos emit no node and do not cut the container; a turn keeps a single process container', () => {
    const items: ChatItem[] = [
      thinking(0, 't'),
      todos(1, [
        { id: 'a', content: 'Read the documents', status: 'completed' },
        { id: 'b', content: 'Draft the design', status: 'in_progress' },
      ]),
      step(2, 'read', false),
    ]
    const nodes = buildNodes(items, true)
    expect(nodes.map((n) => n.kind)).toEqual(['container'])
    const c = nodes[0]
    if (c.kind === 'container') {
      expect(c.running).toBe(true)
      // Neither todos nor thinking enter container.items; only the visible process entry step remains
      expect(c.items.map((i) => i.kind)).toEqual(['step'])
    }
  })
})

describe('buildNodes — background-subtask result notices enter neither the main flow nor the container', () => {
  it('[subagent] Result text is neither folded into the container nor kept as the final result', () => {
    const items: ChatItem[] = [
      user(0, 'question'),
      step(1, 'read'),
      assistant(2, '[subagent] Result from explorer (background task task-x): I searched …'),
      assistant(3, 'This is the real final answer'),
    ]
    const nodes = buildNodes(items, false)
    // user + container(step) + final assistant; the notice is dropped
    expect(nodes.map((n) => n.kind)).toEqual(['item', 'container', 'item'])
    const c = nodes[1]
    if (c.kind === 'container') {
      expect(c.items.map((i) => i.kind)).toEqual(['step'])
    }
    const last = nodes[2]
    if (last.kind === 'item' && last.item.kind === 'assistant') {
      expect(last.item.text).toBe('This is the real final answer')
    }
  })
})

describe('argsPreview — one-line tool-argument preview', () => {
  it('objects flatten to key: value with string values unquoted', () => {
    expect(argsPreview({ path: 'a.md', limit: 20 })).toBe('path: a.md, limit: 20')
  })

  it('squashes newlines and truncates overlong arguments with …', () => {
    const s = argsPreview({ cmd: `echo hi\n${'x'.repeat(100)}` }, 20)
    expect(s).toHaveLength(21)
    expect(s.endsWith('…')).toBe(true)
    expect(s).not.toContain('\n')
  })

  it('empty object / null return an empty string', () => {
    expect(argsPreview({})).toBe('')
    expect(argsPreview(null)).toBe('')
    expect(argsPreview(undefined)).toBe('')
  })
})

describe('collectCitationRefs — turn-level citation/consultation collection', () => {
  const readStep = (seq: number, path: string): ChatItem => ({
    kind: 'step',
    seq,
    callId: `c${seq}`,
    toolName: 'read',
    args: { path },
    result: { success: true, summary: 'ok', output: '' },
  })

  it('read paths merge with body footnotes; cited paths dedup and sort first; the footer mounts on the turn last assistant', async () => {
    const { collectCitationRefs } = await import('./conversationNodes')
    const items: ChatItem[] = [
      user(0, 'Which exposure events exist?'),
      readStep(1, 'knowledge/wiki/video.md'),
      readStep(2, 'knowledge/wiki/live.md'),
      assistant(3, 'Exposure uses video_show [^1].\n\n[^1]: knowledge/wiki/video.md#exposure'),
    ]
    const { allRaws, footerBySeq } = collectCitationRefs(items, false)
    expect(allRaws).toEqual([
      'knowledge/wiki/video.md#exposure',
      'knowledge/wiki/video.md',
      'knowledge/wiki/live.md',
    ])
    const entries = footerBySeq.get(3)!
    // The cited video.md (with anchor) sorts first; read paths of the same file
    // dedup by the anchor-stripped base; live.md was only consulted
    expect(entries).toEqual([
      { raw: 'knowledge/wiki/video.md#exposure', cited: true },
      { raw: 'knowledge/wiki/live.md', cited: false },
    ])
  })

  it('the running final turn shows no footer; earlier turns do as usual', async () => {
    const { collectCitationRefs } = await import('./conversationNodes')
    const items: ChatItem[] = [
      user(0, 'first question'),
      readStep(1, 'knowledge/wiki/a.md'),
      assistant(2, 'answer one'),
      user(3, 'second question'),
      readStep(4, 'knowledge/wiki/b.md'),
      assistant(5, 'answer two (in progress)'),
    ]
    const { footerBySeq } = collectCitationRefs(items, true)
    expect(footerBySeq.has(2)).toBe(true)
    expect(footerBySeq.has(5)).toBe(false)
    // The same list grows a footer once the turn ends (running=false)
    expect(collectCitationRefs(items, false).footerBySeq.has(5)).toBe(true)
  })

  it('subtask tool-step reads count as consultations; a turn without an assistant shows no footer', async () => {
    const { collectCitationRefs } = await import('./conversationNodes')
    const sub: ChatItem = {
      kind: 'subtask',
      seq: 1,
      subtaskId: 's1',
      agentName: 'explorer',
      goal: 'find the events',
      status: 'completed',
      summary: '',
      steps: [
        {
          kind: 'step',
          seq: 0,
          callId: 'sc1',
          toolName: 'read',
          args: { path: 'knowledge/wiki/c.md' },
          result: { success: true, summary: 'ok', output: '' },
        },
      ],
    }
    const items: ChatItem[] = [user(0, 'question'), sub]
    const { allRaws, footerBySeq } = collectCitationRefs(items, false)
    expect(allRaws).toEqual(['knowledge/wiki/c.md'])
    expect(footerBySeq.size).toBe(0)
  })
})
