import { useEffect, useState } from 'react'
import { IconClose } from './icons'

/**
 * Disconnect notice bar: only shown once the SSE connection has been down for
 * >3s (filters the brief interruption of switching sessions); disappears
 * automatically on recovery. Dismissing with × silences the current outage;
 * the dismissal resets after recovery.
 */
export function ReconnectBanner({ active }: { active: boolean }) {
  const [show, setShow] = useState(false)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!active) {
      setShow(false)
      setDismissed(false)
      return
    }
    const timer = window.setTimeout(() => setShow(true), 3000)
    return () => window.clearTimeout(timer)
  }, [active])

  if (!active || !show || dismissed) return null
  return (
    <div className="flex h-9 shrink-0 items-center gap-2 border border-warn/30 bg-warn-soft px-3 text-[12.5px] text-warn">
      <span className="min-w-0 flex-1 truncate">Connection lost — reconnecting…</span>
      <button
        type="button"
        title="Dismiss"
        onClick={() => setDismissed(true)}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-warn/10"
      >
        <IconClose className="h-2.5 w-2.5" />
      </button>
    </div>
  )
}
