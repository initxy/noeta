import { cn } from '../lib/cn'

/** Brand wordmark: the O of NOETA is a "dot" that pulses while the agent is running. */
export function Logo({
  running = false,
  size = 'sm',
}: {
  running?: boolean
  size?: 'sm' | 'lg'
}) {
  return (
    <span
      className={cn(
        'inline-flex select-none items-center font-mono font-semibold tracking-[0.18em] text-ink',
        size === 'sm' ? 'text-[15px]' : 'text-[28px]',
      )}
    >
      N
      <span
        aria-hidden
        className={cn(
          'rail-dot mx-[0.18em] inline-block',
          size === 'lg' && 'h-[14px] w-[14px]',
          running ? 'rail-dot--active' : 'rail-dot--accent',
        )}
      />
      ETA
      <span className="ml-[0.6em] hidden text-[0.72em] font-normal normal-case tracking-normal text-ink-3 sm:inline">
        Agent
      </span>
    </span>
  )
}
