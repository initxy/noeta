import { useState } from 'react'
import type { AdvancePreview } from '../../api/types'
import { IconClose } from '../icons'

/**
 * Advance confirmation dialog (ADR-0012 D11): shows the handoff-prefilled param
 * form for the next node (unextracted values are left empty and marked "fill in")
 * plus an editable handoff summary; confirming starts the next node. Cancel has no
 * side effects — clicking "Next stage" again regenerates.
 */
export function AdvanceDialog({
  preview,
  onConfirm,
  onClose,
}: {
  preview: AdvancePreview
  onConfirm: (params: Record<string, string>, summary: string) => Promise<void>
  onClose: () => void
}) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {}
    for (const p of preview.param_defs) init[p.name] = preview.params[p.name] ?? ''
    return init
  })
  const [summary, setSummary] = useState(preview.summary)
  const [submitting, setSubmitting] = useState(false)

  const missingRequired = preview.param_defs.some(
    (p) => p.required && !(values[p.name] ?? '').trim(),
  )

  const submit = async () => {
    if (missingRequired || submitting) return
    setSubmitting(true)
    try {
      await onConfirm(values, summary)
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
      <div className="msg-enter relative flex max-h-[85vh] w-full max-w-lg flex-col rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]">
        <div className="mb-1 flex shrink-0 items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">
            Next stage · {preview.node_name}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>
        <p className="mb-3 text-[12px] text-ink-3">
          {preview.degraded
            ? 'Automatic handoff generation failed — fill in the params by hand.'
            : 'These params were extracted from the previous stage; review and complete them, then confirm.'}
        </p>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
          {preview.param_defs.map((p) => {
            const extracted = preview.params[p.name] != null
            return (
              <div key={p.name}>
                <label className="mb-1 block text-[12px] text-ink-2">
                  {p.name}
                  {p.required && <span className="ml-0.5 text-danger">*</span>}
                  {p.description && (
                    <span className="ml-1.5 text-ink-3">{p.description}</span>
                  )}
                  {!extracted && (
                    <span className="ml-1.5 font-mono text-[10.5px] text-accent">
                      fill in
                    </span>
                  )}
                </label>
                <textarea
                  value={values[p.name] ?? ''}
                  rows={2}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [p.name]: e.target.value }))
                  }
                  placeholder={extracted ? '' : 'Not extracted from the previous stage — fill in'}
                  className="w-full resize-y rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
                />
              </div>
            )
          })}
          <div>
            <label className="mb-1 block text-[12px] text-ink-2">
              Handoff summary
              <span className="ml-1.5 text-ink-3">
                (injected along with the next stage's start instruction; editable)
              </span>
            </label>
            <textarea
              value={summary}
              rows={6}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="Key conclusions from the previous stage, artifact links, open items…"
              className="w-full resize-y rounded-lg border border-border bg-bg px-3 py-2 text-[13px] leading-relaxed text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
            />
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
            {submitting ? 'Starting…' : 'Confirm and advance'}
          </button>
        </div>
      </div>
    </div>
  )
}
