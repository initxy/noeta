import { useState } from 'react'
import type { AnswerPayload } from '../api/types'
import type { QuestionItemView } from '../chat/useChat'
import { cn } from '../lib/cn'

interface QuestionCardProps {
  item: QuestionItemView
  onSubmit: (questionId: string, answers: AnswerPayload) => Promise<void>
}

/** Follow-up question card: the agent is suspended waiting; the user picks a choice or types freeform to continue. */
export function QuestionCard({ item, onSubmit }: QuestionCardProps) {
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [texts, setTexts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const complete = item.questions.every(
    (q) => choices[q.id] || texts[q.id]?.trim(),
  )

  const submit = async () => {
    if (!complete || busy) return
    setBusy(true)
    setError(null)
    const answers: AnswerPayload = {}
    for (const q of item.questions) {
      if (choices[q.id]) answers[q.id] = { choice_id: choices[q.id] }
      else answers[q.id] = { text: texts[q.id].trim() }
    }
    try {
      await onSubmit(item.questionId, answers)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to submit')
      setBusy(false)
    }
  }

  return (
    <div
      className={cn(
        'rounded-xl border bg-surface p-4',
        item.answered ? 'border-border opacity-70' : 'border-accent/40',
      )}
    >
      <p className="mb-3 flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent">
        <span className={cn('rail-dot', item.answered ? 'rail-dot--done' : 'rail-dot--active')} />
        {item.answered ? 'Answered' : 'Waiting for your input'}
      </p>
      {item.reason ? (
        <p className="mb-3 text-[13px] leading-relaxed text-ink-2">{item.reason}</p>
      ) : null}

      <div className="space-y-4">
        {item.questions.map((q) => (
          <div key={q.id}>
            {q.header && (
              <p className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-3">
                {q.header}
              </p>
            )}
            <p className="mb-2 text-[14px] font-medium text-ink">{q.question}</p>

            {q.choices && q.choices.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {q.choices.map((c) => {
                  const selected = choices[q.id] === c.id
                  return (
                    <button
                      key={c.id}
                      type="button"
                      disabled={item.answered || busy}
                      title={c.description ?? undefined}
                      onClick={() => {
                        setChoices((m) => ({ ...m, [q.id]: selected ? '' : c.id }))
                        if (!selected) setTexts((m) => ({ ...m, [q.id]: '' }))
                      }}
                      className={cn(
                        'rounded-lg border px-3 py-1.5 text-[13px] transition-colors disabled:cursor-not-allowed',
                        selected
                          ? 'border-accent bg-accent-soft font-medium text-ink'
                          : 'border-border bg-bg text-ink-2 hover:border-border-strong hover:text-ink',
                      )}
                    >
                      {c.label}
                    </button>
                  )
                })}
              </div>
            )}

            {(q.allow_freeform || !q.choices || q.choices.length === 0) &&
              !item.answered && (
                <input
                  value={texts[q.id] ?? ''}
                  disabled={busy}
                  onChange={(e) => {
                    setTexts((m) => ({ ...m, [q.id]: e.target.value }))
                    if (e.target.value) setChoices((m) => ({ ...m, [q.id]: '' }))
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void submit()
                  }}
                  placeholder="Or type a custom answer…"
                  className="mt-2 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
                />
              )}
          </div>
        ))}
      </div>

      {error && <p className="mt-3 text-[12.5px] text-danger">{error}</p>}

      {!item.answered && (
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!complete || busy}
          className="mt-4 rounded-lg bg-accent px-4 py-2 text-[13px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Submitting…' : 'Submit and continue'}
        </button>
      )}
    </div>
  )
}
