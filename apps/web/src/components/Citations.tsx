/**
 * Citation-provenance UI: body superscripts (CitationMark, hover excerpt card)
 * + the collapsible "references" footer beneath an answer (ReferencesFooter).
 *
 * Degradation path (ADR-0013): resolve pending / exists=false → a de-emphasized
 * non-interactive superscript (a fabricated source never gets a clickable,
 * trustworthy look); file present but anchor gone → document-level entry plus
 * a "source has been updated" note. Origin links open via onOpenDoc (a new
 * browser tab in this port; the vendor doc-preview panel was dropped).
 */
import { useState } from 'react'
import type { ResolvedKnowledgePath } from '../api/types'
import { cn } from '../lib/cn'
import { IconChevron, IconFile } from './icons'

function openOrigin(url: string, onOpenDoc?: (url: string) => void): void {
  if (onOpenDoc) onOpenDoc(url)
  else window.open(url, '_blank', 'noreferrer')
}

/** Body citation superscript: the rendering of a rewritten `[^n]`. Hover pops the excerpt card; click jumps to the source. */
export function CitationMark({
  label,
  resolved,
  onOpenDoc,
}: {
  label: string
  /** undefined = resolve pending (or the label has no definition) */
  resolved?: ResolvedKnowledgePath
  onOpenDoc?: (url: string) => void
}) {
  const [hover, setHover] = useState(false)
  // Unresolved / invalid source: de-emphasized superscript, no hover, no navigation
  if (!resolved || !resolved.exists) {
    return (
      <sup
        className="mx-0.5 select-none font-mono text-[10px] text-ink-3"
        title={resolved ? 'Invalid citation source' : undefined}
      >
        {label}
      </sup>
    )
  }
  const anchorStale = resolved.anchor !== null && resolved.anchor_found === false
  const clickable = Boolean(resolved.origin_url)
  return (
    <span
      className="relative inline-block"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <sup>
        <button
          type="button"
          onClick={() => {
            if (resolved.origin_url) openOrigin(resolved.origin_url, onOpenDoc)
          }}
          className={cn(
            'mx-0.5 inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-full px-1',
            'bg-accent-soft font-mono text-[10px] leading-none text-accent',
            clickable ? 'cursor-pointer hover:brightness-95' : 'cursor-default',
          )}
          aria-label={`Citation ${resolved.title ?? resolved.path}`}
        >
          {label}
        </button>
      </sup>
      {hover && (
        <span className="absolute bottom-full left-1/2 z-30 mb-1.5 w-72 -translate-x-1/2 rounded-lg border border-border bg-surface p-3 text-left shadow-[var(--shadow)]">
          <span className="block truncate text-[12.5px] font-medium text-ink">
            {resolved.title ?? resolved.path}
          </span>
          <span className="mt-0.5 block truncate font-mono text-[10.5px] text-ink-3">
            {resolved.source_name}
            {resolved.anchor && resolved.anchor_found ? ` · ${resolved.anchor}` : ''}
          </span>
          {resolved.excerpt && (
            <span className="mt-1.5 block max-h-36 overflow-hidden whitespace-pre-wrap border-l-2 border-border pl-2 text-[12px] leading-relaxed text-ink-2">
              {resolved.excerpt}
            </span>
          )}
          {anchorStale && (
            <span className="mt-1.5 block text-[11.5px] text-ink-3">
              The source has been updated; the section anchor no longer resolves
            </span>
          )}
          {clickable && (
            <span className="mt-1.5 block text-[11px] text-accent">
              Click to view the source ↗
            </span>
          )}
        </span>
      )}
    </span>
  )
}

/** One entry of the references footer. */
export interface ReferenceEntry {
  /** Raw path used for the resolve request (may carry a #anchor) */
  raw: string
  /** Whether the body footnotes cite it (sorted first + badged) */
  cited: boolean
  resolved?: ResolvedKnowledgePath
}

/**
 * Turn-level collapsible references footer: mounted beneath the turn's last
 * assistant message. Only shows entries that resolved successfully and whose
 * file exists (exists=false is hidden); cited entries sort first.
 */
export function ReferencesFooter({
  entries,
  onOpenDoc,
}: {
  entries: ReferenceEntry[]
  onOpenDoc?: (url: string) => void
}) {
  const [open, setOpen] = useState(false)
  const visible = entries.filter((e) => e.resolved?.exists)
  if (visible.length === 0) return null
  const cited = visible.filter((e) => e.cited).length
  const label =
    cited > 0
      ? `Cited ${cited} source${cited === 1 ? '' : 's'} · consulted ${visible.length} in total`
      : `Consulted ${visible.length} source${visible.length === 1 ? '' : 's'}`
  return (
    <div className="mt-2 rounded-lg border border-border/70 bg-surface-2/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <IconFile className="h-3.5 w-3.5 shrink-0 text-ink-3" />
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-3">
          {label}
        </span>
        <IconChevron open={open} className="h-3 w-3 shrink-0 text-ink-3" />
      </button>
      {open && (
        <ul className="border-t border-border/60 px-2.5 py-1.5">
          {visible.map((e) => {
            const r = e.resolved!
            const clickable = Boolean(r.origin_url)
            return (
              <li key={e.raw}>
                <button
                  type="button"
                  onClick={() => {
                    if (r.origin_url) openOrigin(r.origin_url, onOpenDoc)
                  }}
                  className={cn(
                    'group flex w-full items-center gap-2 rounded px-1 py-1 text-left',
                    clickable ? 'hover:bg-surface-2' : 'cursor-default',
                  )}
                  title={r.path}
                >
                  <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-2">
                    {r.title ?? r.path}
                  </span>
                  {e.cited && (
                    <span className="shrink-0 rounded bg-accent-soft px-1.5 py-px font-mono text-[10px] text-accent">
                      Cited
                    </span>
                  )}
                  <span className="shrink-0 truncate font-mono text-[10.5px] text-ink-3">
                    {r.source_name}
                  </span>
                  {clickable && (
                    <span className="shrink-0 text-[11px] text-ink-3 opacity-0 transition-opacity group-hover:opacity-100">
                      ↗
                    </span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
