import { describe, it, expect } from 'vitest'
import { buildWorkspaceFileMatcher } from './workspaceFile'

describe('buildWorkspaceFileMatcher', () => {
  const match = buildWorkspaceFileMatcher([
    'tracking-attribution-report.md',
    'handoff/01-plan.md',
    'a/dup.md',
    'b/dup.md',
  ])

  it('exact full relative path hits', () => {
    expect(match('tracking-attribution-report.md')).toBe('tracking-attribution-report.md')
    expect(match('handoff/01-plan.md')).toBe('handoff/01-plan.md')
  })

  it('basename hits when unique across the workspace', () => {
    expect(match('01-plan.md')).toBe('handoff/01-plan.md')
  })

  it('duplicate basenames are ambiguous and never guessed; full paths still hit', () => {
    expect(match('dup.md')).toBeNull()
    expect(match('a/dup.md')).toBe('a/dup.md')
  })

  it('a top-level file sharing a name with a subdirectory file: full-path match wins over the ambiguity mark', () => {
    const m = buildWorkspaceFileMatcher(['report.md', 'sub/report.md'])
    expect(m('report.md')).toBe('report.md')
    expect(m('sub/report.md')).toBe('sub/report.md')
  })

  it('tolerates ./ and the in-container absolute /workspace/ prefix plus surrounding whitespace', () => {
    expect(match('./handoff/01-plan.md')).toBe('handoff/01-plan.md')
    expect(match('/workspace/tracking-attribution-report.md')).toBe(
      'tracking-attribution-report.md',
    )
    expect(match(' tracking-attribution-report.md ')).toBe(
      'tracking-attribution-report.md',
    )
  })

  it('non-file text never hits', () => {
    expect(match('send_btn_show/click')).toBeNull()
    expect(match('is_input_box')).toBeNull()
    expect(match('')).toBeNull()
  })
})
