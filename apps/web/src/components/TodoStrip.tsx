import { useState } from 'react'
import type { TodoItem } from '../api/types'
import { cn } from '../lib/cn'
import { IconChevron } from './icons'

/**
 * Persistent checklist strip above the composer.
 *
 * Collapsed: a one-line summary — progress count + mini progress bar + the
 * current in-progress item; click to expand the full checklist. todos update
 * live via todo_update wholesale replacement. The main flow no longer renders
 * a checklist card — progress lives here permanently and does not scroll away
 * with messages; the parent skips rendering when there are no todos.
 */
export function TodoStrip({ todos }: { todos: TodoItem[] }) {
  const [open, setOpen] = useState(false)
  const done = todos.filter((t) => t.status === 'completed').length
  const allDone = todos.length > 0 && done === todos.length
  const active = todos.find((t) => t.status === 'in_progress')
  return (
    <div className="mb-2 rounded-xl border border-border bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-4 py-2 text-left"
      >
        <span className="shrink-0 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-3">
          Checklist · {done}/{todos.length}
        </span>
        <span
          className="h-1 w-16 shrink-0 overflow-hidden rounded-full bg-surface-2"
          aria-hidden
        >
          <span
            className={cn(
              'block h-full rounded-full transition-[width] duration-300',
              allDone ? 'bg-ink-3' : 'bg-accent',
            )}
            style={{
              width: `${todos.length > 0 ? (done / todos.length) * 100 : 0}%`,
            }}
          />
        </span>
        {active && !open ? (
          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
            <span className="rail-dot rail-dot--accent mr-1.5 inline-block align-middle" aria-hidden />
            {active.content}
          </span>
        ) : (
          <span className="min-w-0 flex-1" aria-hidden />
        )}
        <IconChevron open={open} className="h-3 w-3 shrink-0 text-ink-3" />
      </button>
      {open && (
        <ul className="space-y-1 border-t border-border px-4 py-2.5">
          {todos.map((t) => (
            <li key={t.id} className="flex items-start gap-2 text-[13px] leading-relaxed">
              {t.status === 'in_progress' ? (
                <span className="rail-dot rail-dot--accent mt-1.5 shrink-0" aria-hidden />
              ) : (
                <span className="shrink-0 font-mono text-[11.5px] text-ink-3" aria-hidden>
                  {t.status === 'completed' ? '✓' : '○'}
                </span>
              )}
              <span
                className={cn(
                  t.status === 'completed' && 'text-ink-3 line-through',
                  t.status === 'in_progress' && 'font-medium text-ink',
                  t.status === 'pending' && 'text-ink-2',
                )}
              >
                {t.content}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
