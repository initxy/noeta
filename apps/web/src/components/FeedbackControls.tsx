import { useState } from 'react'
import { feedbackApi } from '../api/endpoints'
import { cn } from '../lib/cn'
import { useToast } from '../state/toast'
import { IconThumbDown, IconThumbUp } from './icons'

/** Preset tags for the thumbs-down popover (aligned with the backend api/feedback.py VALID_TAGS). */
const TAGS = [
  'irrelevant answer',
  'incorrect result',
  'knowledge base not used',
  'wrong citation',
  'too slow',
  'other',
]

/**
 * Feedback controls beneath an assistant message (shown on hover, ADR-0017):
 * 👍 records with a single click; 👎 expands a lightweight form (tag chips +
 * optional comment + optional "correct result" reference text). Feedback rate
 * comes first — every field is skippable and one submit completes the flow.
 * Messages that already have feedback show the submitted rating permanently.
 */
export function FeedbackControls({
  sessionId,
  taskId,
  seq,
  submittedRating,
  onSubmitted,
}: {
  sessionId: string
  taskId?: string | null
  seq: number
  /** Rating already submitted for this message (1/-1); undefined = no feedback yet */
  submittedRating?: number
  onSubmitted: (seq: number, rating: 1 | -1) => void
}) {
  const { toast } = useToast()
  const [formOpen, setFormOpen] = useState(false)
  const [tags, setTags] = useState<string[]>([])
  const [comment, setComment] = useState('')
  const [refOpen, setRefOpen] = useState(false)
  const [refText, setRefText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (rating: 1 | -1) => {
    setSubmitting(true)
    try {
      const { feedback } = await feedbackApi.submit(sessionId, {
        rating,
        task_id: taskId ?? undefined,
        event_seq: seq,
        tags: rating === -1 ? tags : [],
        comment: rating === -1 ? comment.trim() : '',
      })
      onSubmitted(seq, rating)
      setFormOpen(false)
      // The reference is an independent step: the feedback is already stored;
      // a failure here only warns, never rolls the feedback back.
      const text = refText.trim()
      if (rating === -1 && text) {
        try {
          await feedbackApi.putReference(feedback.space_id, feedback.id, {
            kind: 'text',
            text,
          })
          toast('Feedback and reference submitted', 'info')
        } catch (e) {
          toast(
            e instanceof Error
              ? e.message
              : 'Failed to submit the reference; you can add it later on the Feedback page',
            'error',
          )
        }
      } else {
        toast('Thanks for the feedback', 'info')
      }
    } catch {
      toast('Failed to submit feedback', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  if (submittedRating !== undefined) {
    return (
      <div className="mt-1 flex items-center gap-1 text-ink-3">
        {submittedRating === 1 ? (
          <IconThumbUp className="h-3.5 w-3.5 text-accent" />
        ) : (
          <IconThumbDown className="h-3.5 w-3.5 text-accent" />
        )}
        <span className="text-[11px]">Feedback sent</span>
      </div>
    )
  }

  return (
    <div className="mt-1">
      <div
        className={cn(
          'flex items-center gap-0.5 transition-opacity',
          formOpen ? 'opacity-100' : 'opacity-0 group-hover/msg:opacity-100',
        )}
      >
        <button
          type="button"
          title="Helpful"
          disabled={submitting}
          onClick={() => void submit(1)}
          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconThumbUp className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          title="Not great"
          disabled={submitting}
          onClick={() => setFormOpen((v) => !v)}
          className={cn(
            'flex h-6 w-6 items-center justify-center rounded-md transition-colors hover:bg-surface-2 hover:text-ink',
            formOpen ? 'bg-surface-2 text-ink' : 'text-ink-3',
          )}
        >
          <IconThumbDown className="h-3.5 w-3.5" />
        </button>
      </div>

      {formOpen && (
        <div className="mt-1.5 w-full max-w-md rounded-lg border border-border bg-surface p-2.5">
          <p className="text-[12px] font-medium text-ink">What went wrong? (optional)</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {TAGS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() =>
                  setTags((cur) =>
                    cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t],
                  )
                }
                className={cn(
                  'rounded-full border px-2 py-0.5 text-[11px] transition-colors',
                  tags.includes(t)
                    ? 'border-accent/40 bg-accent-soft text-accent'
                    : 'border-border text-ink-2 hover:bg-surface-2',
                )}
              >
                {t}
              </button>
            ))}
          </div>
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            rows={2}
            placeholder="Tell us what went wrong (optional)"
            className="mt-2 w-full resize-y rounded-lg border border-border bg-bg px-2 py-1.5 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
          />
          {refOpen ? (
            <textarea
              value={refText}
              onChange={(e) => setRefText(e.target.value)}
              rows={3}
              placeholder="Paste the correct result text"
              className="mt-1.5 w-full resize-y rounded-lg border border-border bg-bg px-2 py-1.5 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
            />
          ) : (
            <button
              type="button"
              onClick={() => setRefOpen(true)}
              className="mt-1.5 text-[11.5px] text-accent hover:underline"
            >
              + Attach the correct result (most helpful for improvement)
            </button>
          )}
          <div className="mt-2 flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setFormOpen(false)}
              className="flex h-6.5 items-center rounded-lg px-2 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={submitting}
              onClick={() => void submit(-1)}
              className={cn(
                'flex items-center rounded-lg bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-ink transition-opacity hover:opacity-90',
                submitting && 'cursor-not-allowed opacity-50',
              )}
            >
              {submitting ? 'Submitting…' : 'Submit feedback'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
