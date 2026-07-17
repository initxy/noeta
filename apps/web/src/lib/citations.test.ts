import { describe, expect, it } from 'vitest'
import {
  citeLabelFromHref,
  extractKnowledgePaths,
  parseCitationDefs,
  rewriteCitationMarkup,
} from './citations'

const ANSWER = `Video exposure uses the video_show event [^1]; clicks use video_click [^2].

[^1]: knowledge/my-wiki/video/video-events.md#video-exposure
[^2]: knowledge/my-wiki/video/video-events.md`

describe('parseCitationDefs', () => {
  it('parses protocol definitions with and without anchors', () => {
    const defs = parseCitationDefs(ANSWER)
    expect(defs).toHaveLength(2)
    expect(defs[0]).toMatchObject({
      label: '1',
      path: 'knowledge/my-wiki/video/video-events.md',
      anchor: 'video-exposure',
      raw: 'knowledge/my-wiki/video/video-events.md#video-exposure',
    })
    expect(defs[1].anchor).toBeNull()
  })

  it('ignores ordinary footnotes (non-knowledge/ paths)', () => {
    const defs = parseCitationDefs('Body [^a].\n\n[^a]: an ordinary footnote note')
    expect(defs).toHaveLength(0)
  })

  it('keeps only the first of duplicate labels', () => {
    const defs = parseCitationDefs(
      '[^1]: knowledge/w/a.md\n[^1]: knowledge/w/b.md',
    )
    expect(defs).toHaveLength(1)
    expect(defs[0].path).toBe('knowledge/w/a.md')
  })
})

describe('rewriteCitationMarkup', () => {
  it('rewrites references to #cite- links and removes definition lines', () => {
    const defs = parseCitationDefs(ANSWER)
    const out = rewriteCitationMarkup(ANSWER, defs)
    expect(out).toContain('[1](#cite-1)')
    expect(out).toContain('[2](#cite-2)')
    expect(out).not.toContain('[^1]:')
    expect(out).not.toContain('knowledge/my-wiki')
  })

  it('leaves references with undefined labels and ordinary footnotes untouched', () => {
    const text = 'See [^1] and [^x].\n\n[^1]: knowledge/w/a.md\n[^x]: ordinary footnote'
    const out = rewriteCitationMarkup(text, parseCitationDefs(text))
    expect(out).toContain('[1](#cite-1)')
    expect(out).toContain('[^x]')
    expect(out).toContain('[^x]: ordinary footnote')
  })

  it('returns the text unchanged when defs is empty', () => {
    expect(rewriteCitationMarkup('no citations', [])).toBe('no citations')
  })
})

describe('citeLabelFromHref', () => {
  it('recovers the label', () => {
    expect(citeLabelFromHref('#cite-1')).toBe('1')
    expect(citeLabelFromHref('#cite-%C3%A9')).toBe('é')
  })
  it('returns null for non-citation hrefs', () => {
    expect(citeLabelFromHref('#top')).toBeNull()
    expect(citeLabelFromHref(undefined)).toBeNull()
  })
})

describe('extractKnowledgePaths', () => {
  it("takes read's path argument (including ./ and /workspace/ prefixes)", () => {
    expect(extractKnowledgePaths('read', { path: 'knowledge/w/a.md' })).toEqual([
      'knowledge/w/a.md',
    ])
    expect(
      extractKnowledgePaths('read', { path: '/workspace/knowledge/w/a.md' }),
    ).toEqual(['knowledge/w/a.md'])
    expect(extractKnowledgePaths('read', { path: 'report.md' })).toEqual([])
  })

  it('extracts explicit paths from shell_run command strings (directory arguments excluded)', () => {
    const paths = extractKnowledgePaths('shell_run', {
      command:
        "rg -n 'video_show' knowledge/my-wiki/ && cat 'knowledge/my-wiki/video/video-events.md'",
    })
    expect(paths).toEqual(['knowledge/my-wiki/video/video-events.md'])
  })

  it('returns empty for other tools / invalid arguments', () => {
    expect(extractKnowledgePaths('write', { path: 'knowledge/w/a.md' })).toEqual([])
    expect(extractKnowledgePaths('read', null)).toEqual([])
    expect(extractKnowledgePaths('shell_run', { command: 42 })).toEqual([])
  })
})
