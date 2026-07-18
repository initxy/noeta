import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown } from './Markdown'

/**
 * Markdown file-chip integration (SSR render assertions): inline code /
 * relative links that hit a workspace file render as a WorkspaceFileChip
 * (button); misses and block-level code keep the default rendering.
 * react-markdown v10 has no inline flag — inline vs block is distinguished by
 * "no newline"; this suite locks that behavior in.
 */
describe('Markdown workspace-file chips', () => {
  const files = ['final report.md', 'handoff/01-plan.md']
  const render = (text: string) =>
    renderToStaticMarkup(
      <Markdown text={text} workspaceFiles={files} onOpenFile={() => {}} />,
    )

  it('inline code hitting a file → chip (title carries the full path)', () => {
    const html = render('See `final report.md`.')
    expect(html).toContain('<button')
    expect(html).toContain('Preview final report.md')
    expect(html).not.toContain('<code>final report.md</code>')
  })

  it('a unique basename hits the file in a subdirectory', () => {
    const html = render('Handoff draft `01-plan.md`')
    expect(html).toContain('Preview handoff/01-plan.md')
  })

  it('inline code that misses keeps the default rendering', () => {
    const html = render('Event `send_btn_show/click`')
    expect(html).toContain('<code>send_btn_show/click</code>')
    expect(html).not.toContain('<button')
  })

  it('block-level code renders no chip even when its content is a file path', () => {
    const html = render('```\nfinal report.md\n```')
    expect(html).not.toContain('<button')
    expect(html).toContain('<pre>')
  })

  it('a relative link hitting a file → chip (including a URL-encoded href)', () => {
    const html = render('[report](final%20report.md)')
    expect(html).toContain('Preview final report.md')
  })

  it('without workspaceFiles/onOpenFile inline code renders by default', () => {
    const html = renderToStaticMarkup(
      <Markdown text={'`final report.md`'} />,
    )
    expect(html).toContain('<code>final report.md</code>')
  })
})
