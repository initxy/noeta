/** Clickable chip for a ContentRef: collapsed it shows label/media_type/size/shortHash;
 *  expanded it derefs the body (JSON via JsonTree, nested ContentRefs recurse into
 *  chips, long text clamps). */
import { useMemo, useState } from 'react'
import { cn } from '../../lib/cn'
import { IconChevron } from '../icons'
import { fmtSize, isContentRef, type ContentRefJson } from './model'
import { useContentBody } from './useContentBody'

const CLIP_CHARS = 16 * 1024

interface RefChipProps {
  refJson: ContentRefJson
  /** Chip prefix label (e.g. request / response / plan). */
  label?: string
  className?: string
}

export function RefChip({ refJson, label, className }: RefChipProps) {
  const [open, setOpen] = useState(false)
  return (
    <div className={cn('min-w-0', className)}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2 py-0.5 font-mono text-[10.5px] transition-colors hover:border-accent',
          open ? 'text-ink' : 'text-ink-2',
        )}
      >
        <IconChevron open={open} className="h-2.5 w-2.5 shrink-0 text-ink-3" />
        {label && <span className="shrink-0 font-medium text-ink">{label}</span>}
        <span className="shrink-0">{refJson.media_type}</span>
        <span className="shrink-0 text-ink-3">{fmtSize(refJson.size)}</span>
        <span className="truncate text-ink-3">{refJson.hash.slice(0, 8)}</span>
      </button>
      {open && <RefBody refJson={refJson} />}
    </div>
  )
}

function RefBody({ refJson }: { refJson: ContentRefJson }) {
  const { body, loading, error } = useContentBody(refJson.hash)
  const parsed = useMemo(() => {
    if (body == null || !refJson.media_type.includes('json')) return undefined
    try {
      return JSON.parse(body) as unknown
    } catch {
      return undefined
    }
  }, [body, refJson.media_type])

  if (loading) {
    return <p className="mt-1 pl-2 font-mono text-[11px] text-ink-3">Loading…</p>
  }
  if (error) {
    return <p className="mt-1 pl-2 font-mono text-[11px] text-danger">{error}</p>
  }
  if (body == null) return null
  return (
    <div className="mt-1 overflow-x-auto rounded-lg border border-border bg-surface-2 p-2.5">
      {parsed !== undefined ? (
        <JsonTree value={parsed} />
      ) : (
        <ClampText text={body} />
      )}
    </div>
  )
}

/** Text over 16KB clamps; click to expand fully. */
export function ClampText({ text }: { text: string }) {
  const [full, setFull] = useState(false)
  const clipped = !full && text.length > CLIP_CHARS
  return (
    <div className="min-w-0">
      <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed text-ink-2">
        {clipped ? text.slice(0, CLIP_CHARS) : text}
      </pre>
      {clipped && (
        <button
          type="button"
          onClick={() => setFull(true)}
          className="mt-1 font-mono text-[10.5px] text-accent hover:underline"
        >
          Show all ({fmtSize(text.length)})
        </button>
      )}
    </div>
  )
}

// ---- JsonTree: recursive JSON rendering; ContentRef nodes render as expandable chips ----

function JsonScalar({ value }: { value: unknown }) {
  if (typeof value === 'string') {
    // Long strings (e.g. a system prompt) skip JSON escaping so newlines stay readable.
    if (value.length > 120 || value.includes('\n')) {
      return (
        <div className="my-0.5 rounded border border-border bg-bg px-1.5 py-1">
          <ClampText text={value} />
        </div>
      )
    }
    return <span className="text-ink-2">"{value}"</span>
  }
  return <span className="text-accent">{JSON.stringify(value)}</span>
}

export function JsonTree({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (isContentRef(value)) return <RefChip refJson={value} />
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-ink-3">[]</span>
    return (
      <div className={cn(depth > 0 && 'border-l border-border pl-3')}>
        {value.map((v, i) => (
          <div key={i} className="flex gap-1.5 py-px">
            <span className="shrink-0 font-mono text-[10.5px] text-ink-3">{i}</span>
            <div className="min-w-0 flex-1 font-mono text-[11.5px]">
              <JsonTree value={v} depth={depth + 1} />
            </div>
          </div>
        ))}
      </div>
    )
  }
  if (typeof value === 'object' && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="text-ink-3">{'{}'}</span>
    return (
      <div className={cn(depth > 0 && 'border-l border-border pl-3')}>
        {entries.map(([k, v]) => (
          <div key={k} className="flex gap-1.5 py-px">
            <span className="shrink-0 font-mono text-[11px] text-ink-3">{k}:</span>
            <div className="min-w-0 flex-1 font-mono text-[11.5px]">
              <JsonTree value={v} depth={depth + 1} />
            </div>
          </div>
        ))}
      </div>
    )
  }
  return <JsonScalar value={value} />
}
