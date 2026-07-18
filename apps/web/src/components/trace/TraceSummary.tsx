/** Trace top summary bar (spans all three columns): model / event count / LLM
 *  rounds / tokens / cost / total duration. All folded from raw events (traceTotals). */
import { useMemo } from 'react'
import type { RawEnvelope } from '../../api/types'
import { fmtDuration, fmtTokens, traceTotals } from './model'

function Stat({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5">
      <span className="shrink-0 text-[11px] text-ink-3">{label}</span>
      <span
        className={
          'truncate text-[12px] text-ink' + (mono ? ' font-mono' : '')
        }
        title={value}
      >
        {value}
      </span>
    </div>
  )
}

export function TraceSummary({ events }: { events: RawEnvelope[] }) {
  const t = useMemo(() => traceTotals(events), [events])
  if (events.length === 0) return null
  const cacheRate =
    t.cacheRead > 0 && t.tokensIn > 0
      ? ` (cache ${Math.round((t.cacheRead / t.tokensIn) * 100)}%)`
      : ''
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1 border-b border-border bg-surface px-4 py-2">
      {t.model && <Stat label="model" value={t.model} />}
      <Stat label="events" value={String(t.events)} />
      <Stat label="LLM rounds" value={String(t.rounds)} />
      {t.subagents > 0 && <Stat label="subagents" value={String(t.subagents)} />}
      {t.summaryCompactions > 0 && (
        <Stat label="summary compactions" value={String(t.summaryCompactions)} />
      )}
      <Stat
        label="tokens"
        value={`${fmtTokens(t.tokensIn)} in · ${fmtTokens(t.tokensOut)} out${cacheRate}`}
      />
      {t.costUsd > 0 && <Stat label="cost" value={`$${t.costUsd.toFixed(4)}`} />}
      <Stat label="duration" value={fmtDuration(t.durationS)} />
    </div>
  )
}
