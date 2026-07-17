import { useState } from 'react'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import { IconClose } from './icons'

interface Props {
  onClose: () => void
}

/** Create a team space: name required, description optional; on success SpaceProvider switches to it automatically. */
export function CreateSpaceModal({ onClose }: Props) {
  const { createSpace } = useSpace()
  const { toast } = useToast()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    const trimmed = name.trim()
    if (!trimmed || submitting) return
    setSubmitting(true)
    try {
      await createSpace(trimmed, description.trim() || undefined)
      onClose()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to create space')
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
      <div className="msg-enter relative w-full max-w-md rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">Create space</h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>
        <label className="mb-1 block text-[12px] text-ink-2">Space name</label>
        <input
          value={name}
          autoFocus
          maxLength={64}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void submit()
          }}
          placeholder="e.g. Data Platform team"
          className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
        />
        <label className="mb-1 block text-[12px] text-ink-2">
          Description<span className="text-ink-3"> (optional)</span>
        </label>
        <textarea
          value={description}
          maxLength={500}
          rows={3}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this space is for"
          className="mb-4 w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={submitting || !name.trim()}
            onClick={() => void submit()}
            className="rounded-lg bg-accent px-3 py-1.5 text-[13px] font-medium text-accent-ink disabled:opacity-40"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  )
}
