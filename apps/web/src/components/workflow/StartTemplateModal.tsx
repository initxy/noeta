import { useMemo, useState } from 'react'
import type { TemplateParam } from '../../api/types'
import { splitPrompt } from '../../lib/templatePrompt'
import { IconClose } from '../icons'

/**
 * Template / workflow start form (ADR-0012): the full instruction prompt on the
 * left (placeholders highlighted, live-filled with the values typed on the right),
 * the param form on the right → creates the session. Starting = the substituted
 * prompt becomes the new session's first message. Single template = that template's
 * params; workflow = the first node's template params. Without params the caller
 * starts directly and this dialog never opens.
 */
export function StartTemplateModal({
  title,
  description,
  prompt,
  nodeNames,
  params,
  onSubmit,
  onClose,
}: {
  title: string
  description?: string
  /** Display prompt: workflows pass the first node's template prompt. */
  prompt: string
  /** Workflow node-name chain (omitted for single templates). */
  nodeNames?: string[]
  params: TemplateParam[]
  onSubmit: (values: Record<string, string>) => Promise<void>
  onClose: () => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const segments = useMemo(() => splitPrompt(prompt), [prompt])

  const missingRequired = params.some(
    (p) => p.required && !(values[p.name] ?? '').trim(),
  )

  const submit = async () => {
    if (missingRequired || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(values)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/40"
      />
      <div className="msg-enter relative flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]">
        <div className="mb-1 flex shrink-0 items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <h2 className="min-w-0 truncate text-[15px] font-semibold text-ink">
              {title}
            </h2>
            {nodeNames && nodeNames.length > 0 && (
              <span className="shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-accent">
                Workflow · {nodeNames.length} {nodeNames.length === 1 ? 'node' : 'nodes'}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>
        {description && (
          <p className="shrink-0 text-[12.5px] text-ink-3">{description}</p>
        )}
        {nodeNames && nodeNames.length > 0 && (
          <p className="mt-1 shrink-0 font-mono text-[11px] text-ink-3">
            {nodeNames.join(' → ')} (fill in the first node's params)
          </p>
        )}
        <div className="mt-3 grid min-h-0 flex-1 gap-5 overflow-y-auto pr-1 md:grid-cols-[minmax(0,1fr)_260px]">
          {/* Left column: instruction preview; placeholders live-fill with the typed values. */}
          <div className="min-w-0">
            <p className="mb-1 text-[12px] text-ink-2">Instruction</p>
            <div className="whitespace-pre-wrap break-words rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink-2">
              {segments.map((s, i) =>
                s.kind === 'param' ? (
                  <mark
                    key={i}
                    className="rounded bg-accent-soft px-1 text-accent"
                  >
                    {(values[s.name] ?? '').trim() || s.raw}
                  </mark>
                ) : (
                  s.text
                ),
              )}
            </div>
          </div>
          {/* Right column: the param form. */}
          <div>
            <p className="mb-1 text-[12px] text-ink-2">Params</p>
            <div className="space-y-3">
              {params.map((p) => (
                <div key={p.name}>
                  <label className="mb-1 block text-[12px] text-ink-2">
                    <span className="font-mono">{p.name}</span>
                    {p.required && <span className="ml-0.5 text-danger">*</span>}
                    {p.description && (
                      <span className="ml-1.5 text-ink-3">{p.description}</span>
                    )}
                  </label>
                  <textarea
                    value={values[p.name] ?? ''}
                    rows={2}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [p.name]: e.target.value }))
                    }
                    placeholder="Fill in"
                    className="w-full resize-y rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
        <div className="mt-4 flex shrink-0 justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={missingRequired || submitting}
            className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  )
}
