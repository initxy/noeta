import type { WorkflowView } from '../../api/types'
import { cn } from '../../lib/cn'

/**
 * Workflow session node tab bar (ADR-0012):
 * - one tab per started node; click to switch (per-tab independent conversation
 *   stream, switch back anytime to continue);
 * - not-yet-started nodes are grayed out and unclickable;
 * - trailing "Advance" button: enabled while unstarted nodes remain and the
 *   current source node is not running.
 */
export function TaskTabs({
  workflow,
  activeTaskId,
  onSelect,
  onAdvance,
  advancing,
}: {
  workflow: WorkflowView
  activeTaskId: string | null
  onSelect: (taskId: string) => void
  onAdvance: () => void
  advancing: boolean
}) {
  const nodes = workflow.nodes
  const started = nodes.filter((n) => n.task_id)
  const nextIndex = started.length < nodes.length ? started.length : null
  const lastStarted = started[started.length - 1] ?? null
  const canAdvance =
    nextIndex !== null && lastStarted !== null && lastStarted.status !== 'running'

  return (
    <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border px-4 py-2 sm:px-6">
      {nodes.map((n) => {
        const startable = !!n.task_id
        const active = startable && n.task_id === activeTaskId
        return (
          <button
            key={n.index}
            type="button"
            disabled={!startable}
            onClick={() => n.task_id && onSelect(n.task_id)}
            title={n.description || n.name}
            className={cn(
              'flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors',
              active
                ? 'border-border-strong bg-surface-2 font-medium text-ink'
                : startable
                  ? 'border-border text-ink-2 hover:bg-surface-2 hover:text-ink'
                  : 'cursor-not-allowed border-dashed border-border text-ink-3 opacity-60',
            )}
          >
            <span
              className={cn(
                'rail-dot',
                n.status === 'running' && 'rail-dot--active',
                n.status === 'waiting' && 'rail-dot--active',
                n.status === 'idle' && 'rail-dot--done',
              )}
            />
            <span className="font-mono text-[10.5px] text-ink-3">{n.index + 1}</span>
            {n.name}
            {n.status === 'waiting' && (
              <span className="font-mono text-[10px] text-accent">needs answer</span>
            )}
          </button>
        )
      })}
      {nextIndex !== null && (
        <button
          type="button"
          onClick={onAdvance}
          disabled={!canAdvance || advancing}
          title={
            !canAdvance
              ? 'The current node is running; advance once it finishes'
              : `Generate the handoff and enter "${nodes[nextIndex]?.name}"`
          }
          className="ml-auto flex shrink-0 items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {advancing ? (
            <>
              {/* On an accent background, rail-dot--active (accent-colored) would be
                  invisible — use a foreground-colored pulsing dot instead. */}
              <span className="rail-dot animate-pulse bg-accent-ink" />
              Generating handoff…
            </>
          ) : (
            <>Next stage · {nodes[nextIndex]?.name} →</>
          )}
        </button>
      )}
    </div>
  )
}
