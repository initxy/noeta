import type { SyncProgress } from '../api/types'

/** Phase → completion fraction for the stepped progress bar (the backend
 * reports coarse phases, not per-file counts). */
const PHASE_PCT: Record<string, number> = {
  starting: 10,
  cloned: 45,
  fetched: 45,
  copying: 75,
  done: 100,
}

/** Human-readable label for the current sync phase. */
function phaseLabel(progress: SyncProgress): string {
  const commit = progress.commit ? ` · ${progress.commit.slice(0, 8)}` : ''
  const files =
    progress.file_count != null
      ? ` · ${progress.file_count} file${progress.file_count === 1 ? '' : 's'}`
      : ''
  switch (progress.phase) {
    case 'starting':
      return 'Starting sync…'
    case 'cloned':
      return `Repository cloned${commit}`
    case 'fetched':
      return `Fetched latest changes${commit}`
    case 'copying':
      return `Copying files…${files}`
    case 'done':
      return `Sync complete${files}`
    default:
      return progress.phase
  }
}

/** Sync progress bar: coarse phase progress (starting / cloned / fetched /
 * copying / done) — a stepped bar plus a phase label with optional commit /
 * file-count detail. */
export function SyncProgressBar({
  progress,
  compact = false,
}: {
  progress: SyncProgress | null
  compact?: boolean
}) {
  if (!progress) {
    return <p className="text-[11px] text-ink-3">Starting sync…</p>
  }
  const pct = PHASE_PCT[progress.phase] ?? 10
  return (
    <div className={compact ? 'space-y-1' : 'space-y-1.5'}>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-[11px] text-ink-3">{phaseLabel(progress)}</p>
    </div>
  )
}
