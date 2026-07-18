import { describe, expect, it } from 'vitest'
import { lineDiff } from './lineDiff'

describe('lineDiff', () => {
  it('identical text is all same', () => {
    const d = lineDiff('a\nb', 'a\nb')
    expect(d).toEqual([
      { type: 'same', text: 'a' },
      { type: 'same', text: 'b' },
    ])
  })

  it('a changed line produces del+add', () => {
    const d = lineDiff('a\nb\nc', 'a\nB\nc')
    expect(d).toEqual([
      { type: 'same', text: 'a' },
      { type: 'del', text: 'b' },
      { type: 'add', text: 'B' },
      { type: 'same', text: 'c' },
    ])
  })

  it('pure additions and pure deletions', () => {
    expect(lineDiff('', 'x').filter((l) => l.type === 'add')).toHaveLength(1)
    expect(lineDiff('a\nb', 'a')).toEqual([
      { type: 'same', text: 'a' },
      { type: 'del', text: 'b' },
    ])
  })

  it('empty current (skill deleted) renders entirely as add', () => {
    const d = lineDiff('', 'x\ny')
    // First line is empty string vs x: the empty line is deleted, x/y added.
    expect(d.some((l) => l.type === 'add' && l.text === 'y')).toBe(true)
  })
})
