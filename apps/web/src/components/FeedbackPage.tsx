import { useCallback, useEffect, useRef, useState } from 'react'
import { feedbackApi } from '../api/endpoints'
import { ApiError } from '../api/client'
import type {
  FeedbackCounts,
  FeedbackEntry,
  FeedbackReport,
  FeedbackRun,
  FeedbackSuggestion,
} from '../api/types'
import { cn } from '../lib/cn'
import { lineDiff } from '../lib/lineDiff'
import { relativeTime } from '../lib/time'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import {
  IconChevron,
  IconThumbDown,
  IconThumbUp,
} from './icons'

/** channel → badge label (the suggestion's landing channel, ADR-0017 multi-channel adoption). */
const CHANNEL_LABELS: Record<string, string> = {
  memory: 'Write memory',
  skill: 'Edit skill',
  report: 'Needs human decision',
}

const STATUS_LABELS: Record<string, string> = {
  adopted: 'Adopted',
  dismissed: 'Dismissed',
}

/**
 * Feedback page (layout matches MemoriesPage): positive/negative counts +
 * improvement-suggestion cards + feedback list. Members can view and attach
 * references; "Generate suggestions" and adopt / dismiss are owner only.
 * While an analysis run is in progress, poll every 2s; refresh suggestions when it ends.
 */
export function FeedbackPage({
  onOpenSession,
}: {
  onOpenSession?: (sessionId: string) => void
}) {
  const { currentSpaceId, currentSpace } = useSpace()
  const { toast } = useToast()
  const [feedback, setFeedback] = useState<FeedbackEntry[]>([])
  const [counts, setCounts] = useState<FeedbackCounts>({ positive: 0, negative: 0 })
  const [suggestions, setSuggestions] = useState<FeedbackSuggestion[]>([])
  const [reports, setReports] = useState<FeedbackReport[]>([])
  const [run, setRun] = useState<FeedbackRun | null>(null)
  const [loading, setLoading] = useState(true)
  // Report selection (the owner picks suggestions to aggregate into one report).
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const pollRef = useRef<number | null>(null)

  const isOwner = currentSpace?.my_role === 'owner'

  const refresh = useCallback(async () => {
    if (!currentSpaceId) return
    try {
      const [fb, sug, rep, latest] = await Promise.all([
        feedbackApi.list(currentSpaceId),
        feedbackApi.suggestions(currentSpaceId),
        feedbackApi.reports(currentSpaceId),
        feedbackApi.latestRun(currentSpaceId),
      ])
      setFeedback(fb.feedback)
      setCounts(fb.counts)
      setSuggestions(sug.suggestions)
      setReports(rep.reports)
      setRun(latest.run)
    } catch {
      toast('Failed to load feedback data', 'error')
    } finally {
      setLoading(false)
    }
  }, [currentSpaceId, toast])

  useEffect(() => {
    setLoading(true)
    void refresh()
  }, [refresh])

  // Poll while a run is in progress; on done/failed refresh everything
  // (suggestions and the analyzed markers both change).
  useEffect(() => {
    if (run?.status !== 'running' || !currentSpaceId) return
    pollRef.current = window.setInterval(async () => {
      try {
        const { run: latest } = await feedbackApi.latestRun(currentSpaceId)
        if (latest?.status !== 'running') {
          if (latest?.status === 'failed') toast('Analysis failed — you can trigger it again', 'error')
          void refresh()
        } else {
          setRun(latest)
        }
      } catch {
        /* Polling failure: try again next round. */
      }
    }, 2000)
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current)
    }
  }, [run?.status, currentSpaceId, refresh, toast])

  const analyze = async () => {
    if (!currentSpaceId) return
    try {
      const { run: created, feedback_count } = await feedbackApi.analyze(
        currentSpaceId,
      )
      setRun(created)
      toast(`Started analyzing ${feedback_count} negative feedback entr${feedback_count === 1 ? 'y' : 'ies'}`, 'info')
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Failed to trigger analysis', 'error')
    }
  }

  const generateReport = async () => {
    if (!currentSpaceId || selected.size === 0) return
    try {
      const { run: created } = await feedbackApi.generateReport(
        currentSpaceId,
        [...selected],
      )
      setRun(created)
      setSelected(new Set())
      toast('Report generation started', 'info')
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Failed to generate report', 'error')
    }
  }

  const running = run?.status === 'running'
  const pending = suggestions.filter((s) => s.status === 'pending')
  const decided = suggestions.filter((s) => s.status !== 'pending')

  return (
    <div className="flex h-full w-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-8">
          <h1 className="text-[20px] font-semibold text-ink">Feedback</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
            Members' thumbs up/down on AI replies collect here. When the owner triggers
            an analysis, a background agent attributes the causes and produces
            improvement suggestions; feedback with a "correct result" reference
            attributes most accurately.
          </p>

          {/* Toolbar: counts + generate suggestions */}
          <div className="mt-4 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 font-mono text-[11.5px] text-ink-3">
              <span className="flex items-center gap-1">
                <IconThumbUp className="h-3 w-3" />
                {counts.positive}
              </span>
              <span className="flex items-center gap-1">
                <IconThumbDown className="h-3 w-3" />
                {counts.negative}
              </span>
              {running && (
                <span className="text-accent">
                  {run?.kind === 'report' ? 'Generating report…' : 'Analysis in progress…'}
                </span>
              )}
            </div>
            {isOwner && (
              <button
                type="button"
                disabled={running}
                onClick={() => void analyze()}
                className={cn(
                  'flex h-7 items-center rounded-lg bg-accent px-3 text-[12px] font-medium text-accent-ink transition-opacity hover:opacity-90',
                  running && 'cursor-not-allowed opacity-50',
                )}
              >
                {running ? 'In progress…' : 'Generate suggestions'}
              </button>
            )}
          </div>

          {/* Improvement suggestions */}
          <div className="mt-6 flex items-center justify-between gap-3">
            <h2 className="text-[14px] font-semibold text-ink">Suggestions</h2>
            {isOwner && selected.size > 0 && (
              <button
                type="button"
                disabled={running}
                onClick={() => void generateReport()}
                className={cn(
                  'flex h-6.5 items-center rounded-lg border border-accent/40 bg-accent-soft px-2.5 text-[12px] font-medium text-accent transition-colors hover:bg-accent/15',
                  running && 'cursor-not-allowed opacity-50',
                )}
              >
                Aggregate {selected.size} into a report
              </button>
            )}
          </div>
          {loading ? (
            <div className="mt-3 h-20 animate-pulse rounded-lg bg-surface-2" />
          ) : suggestions.length === 0 ? (
            <p className="mt-3 text-[12.5px] leading-relaxed text-ink-3">
              No suggestions yet. Once negative feedback accumulates, the owner clicks
              "Generate suggestions" to attribute it in bulk.
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {[...pending, ...decided].map((s) => (
                <SuggestionCard
                  key={s.id}
                  suggestion={s}
                  spaceId={currentSpaceId!}
                  isOwner={isOwner}
                  selected={selected.has(s.id)}
                  onToggleSelect={
                    isOwner
                      ? () =>
                          setSelected((cur) => {
                            const next = new Set(cur)
                            if (next.has(s.id)) next.delete(s.id)
                            else next.add(s.id)
                            return next
                          })
                      : undefined
                  }
                  onChanged={() => void refresh()}
                />
              ))}
            </ul>
          )}

          {/* Reports: artifacts of report-mode runs (draft preview → owner publishes). */}
          {reports.length > 0 && (
            <>
              <h2 className="mt-8 text-[14px] font-semibold text-ink">Reports</h2>
              <ul className="mt-3 space-y-2">
                {reports.map((r) => (
                  <ReportRow
                    key={r.id}
                    report={r}
                    spaceId={currentSpaceId!}
                    isOwner={isOwner}
                    onChanged={() => void refresh()}
                  />
                ))}
              </ul>
            </>
          )}

          {/* Feedback list */}
          <h2 className="mt-8 text-[14px] font-semibold text-ink">All feedback</h2>
          {loading ? (
            <div className="mt-3 space-y-2">
              {[0, 1].map((i) => (
                <div key={i} className="h-14 animate-pulse rounded-lg bg-surface-2" />
              ))}
            </div>
          ) : feedback.length === 0 ? (
            <p className="mt-3 py-6 text-center text-[12.5px] leading-relaxed text-ink-3">
              No feedback yet. Hover over an AI reply in a session to leave a thumbs up or down.
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {feedback.map((fb) => (
                <FeedbackRow
                  key={fb.id}
                  entry={fb}
                  spaceId={currentSpaceId!}
                  onOpenSession={onOpenSession}
                  onChanged={() => void refresh()}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}

function SuggestionCard({
  suggestion: s,
  spaceId,
  isOwner,
  selected,
  onToggleSelect,
  onChanged,
}: {
  suggestion: FeedbackSuggestion
  spaceId: string
  isOwner: boolean
  selected?: boolean
  onToggleSelect?: () => void
  onChanged: () => void
}) {
  const { toast } = useToast()
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  // memory-channel adopt editor (the owner may revise the memory draft before confirming).
  const [adoptOpen, setAdoptOpen] = useState(false)
  const [memoryName, setMemoryName] = useState('')
  const [memoryText, setMemoryText] = useState('')
  // skill-channel diff preview (lazily loaded once on expand).
  const [diffOpen, setDiffOpen] = useState(false)
  const [diff, setDiff] = useState<{ current: string; patched: string } | null>(
    null,
  )
  const [busy, setBusy] = useState(false)

  const hasPatch = s.channel === 'skill' && !!s.skill_patch

  useEffect(() => {
    if (!diffOpen || diff !== null) return
    feedbackApi
      .skillDiff(spaceId, s.id)
      .then((d) => setDiff({ current: d.current, patched: d.patched }))
      .catch(() => toast('Failed to load the skill change preview', 'error'))
  }, [diffOpen, diff, spaceId, s.id, toast])

  const adopt = async () => {
    setBusy(true)
    try {
      if (s.channel === 'memory') {
        await feedbackApi.adopt(spaceId, s.id, {
          memory_name: memoryName.trim(),
          memory_text: memoryText.trim(),
        })
        toast('Adopted and written into space memory', 'info')
      } else if (hasPatch) {
        await feedbackApi.adopt(spaceId, s.id, {})
        toast('Change applied to SKILL.md (the original file was backed up)', 'info')
      } else {
        await feedbackApi.adopt(spaceId, s.id, {})
        toast('Marked adopted (carry out the suggested action manually)', 'info')
      }
      onChanged()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Adopt failed', 'error')
    } finally {
      setBusy(false)
    }
  }

  const dismiss = async () => {
    setBusy(true)
    try {
      await feedbackApi.dismiss(spaceId, s.id)
      toast('Dismissed', 'info')
      onChanged()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Dismiss failed', 'error')
    } finally {
      setBusy(false)
    }
  }

  const decided = s.status !== 'pending'
  return (
    <li
      className={cn(
        'rounded-lg border border-border bg-surface px-3 py-2.5',
        decided && 'opacity-70',
      )}
    >
      <div className="flex items-center gap-2">
        {onToggleSelect && (
          <input
            type="checkbox"
            checked={!!selected}
            onChange={onToggleSelect}
            title="Select to aggregate into a report"
            className="h-3.5 w-3.5 shrink-0 accent-accent"
          />
        )}
        <span className="shrink-0 rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
          {CHANNEL_LABELS[s.channel] ?? s.channel}
        </span>
        {s.skill_name && (
          <span className="shrink-0 font-mono text-[11px] text-ink-2">
            {s.skill_name}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
          {s.title}
        </span>
        {decided && (
          <span className="shrink-0 text-[11px] text-ink-3">
            {STATUS_LABELS[s.status] ?? s.status}
            {s.adopted_result?.memory && ` · ${s.adopted_result.memory}`}
            {s.adopted_result?.skill && ` · applied ${s.adopted_result.skill}`}
          </span>
        )}
      </div>
      <p className="mt-1.5 whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-2">
        {s.body}
      </p>
      {/* Evidence (a required field): collapsible list of the referenced feedback and rationale. */}
      <button
        type="button"
        onClick={() => setEvidenceOpen((v) => !v)}
        className="mt-1.5 flex items-center gap-1 text-[11.5px] text-ink-3 hover:text-ink"
      >
        <IconChevron open={evidenceOpen} className="h-3 w-3" />
        Evidence ({s.evidence.length} feedback entr{s.evidence.length === 1 ? 'y' : 'ies'})
      </button>
      {evidenceOpen && (
        <ul className="mt-1 space-y-1 border-l-2 border-border pl-2.5">
          {s.evidence.map((e, i) => (
            <li key={i} className="text-[11.5px] leading-relaxed text-ink-3">
              <span className="font-mono">{e.feedback_id.slice(0, 8)}</span> ·{' '}
              {e.note}
            </li>
          ))}
        </ul>
      )}
      {/* skill-channel change preview: line-level diff (one-click apply after backup). */}
      {hasPatch && (
        <>
          <button
            type="button"
            onClick={() => setDiffOpen((v) => !v)}
            className="mt-1.5 flex items-center gap-1 text-[11.5px] text-ink-3 hover:text-ink"
          >
            <IconChevron open={diffOpen} className="h-3 w-3" />
            View SKILL.md change
          </button>
          {diffOpen &&
            (diff === null ? (
              <div className="mt-1 h-16 animate-pulse rounded-lg bg-surface-2" />
            ) : (
              <div className="mt-1 max-h-72 overflow-auto rounded-lg border border-border bg-bg p-2 font-mono text-[11.5px] leading-relaxed">
                {lineDiff(diff.current, diff.patched).map((l, i) => (
                  <div
                    key={i}
                    className={cn(
                      'whitespace-pre-wrap break-words px-1',
                      l.type === 'add' && 'bg-accent-soft text-ink',
                      l.type === 'del' && 'bg-danger-soft text-ink-3 line-through',
                      l.type === 'same' && 'text-ink-2',
                    )}
                  >
                    {l.type === 'add' ? '+ ' : l.type === 'del' ? '- ' : '  '}
                    {l.text}
                  </div>
                ))}
              </div>
            ))}
        </>
      )}
      {isOwner && !decided && (
        <div className="mt-2">
          {adoptOpen && s.channel === 'memory' ? (
            <div className="rounded-lg border border-border bg-bg p-2">
              <input
                value={memoryName}
                onChange={(e) => setMemoryName(e.target.value)}
                placeholder="Memory name (kebab-case, e.g. conclusion-check-rule)"
                className="w-full rounded-lg border border-border bg-surface px-2 py-1.5 font-mono text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
              />
              <textarea
                value={memoryText}
                onChange={(e) => setMemoryText(e.target.value)}
                rows={5}
                className="mt-1.5 w-full resize-y rounded-lg border border-border bg-surface px-2 py-1.5 font-mono text-[12px] leading-relaxed text-ink outline-none focus:border-accent"
              />
              <div className="mt-1.5 flex items-center justify-end gap-1.5">
                <button
                  type="button"
                  onClick={() => setAdoptOpen(false)}
                  className="rounded-lg px-2 py-1 text-[12px] text-ink-2 hover:bg-surface-2"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={busy || !memoryName.trim() || !memoryText.trim()}
                  onClick={() => void adopt()}
                  className={cn(
                    'rounded-lg bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-ink hover:opacity-90',
                    (busy || !memoryName.trim() || !memoryText.trim()) &&
                      'cursor-not-allowed opacity-50',
                  )}
                >
                  Write memory
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (s.channel === 'memory') {
                    // The suggestion body is the memory draft: prefill and let the owner revise.
                    setMemoryText(s.body)
                    setAdoptOpen(true)
                  } else if (hasPatch) {
                    if (
                      window.confirm(
                        `This applies the change to SKILL.md of the space skill "${s.skill_name}" (the original file is backed up automatically; only new sessions are affected). Continue?`,
                      )
                    )
                      void adopt()
                  } else {
                    void adopt()
                  }
                }}
                className="flex h-6.5 items-center rounded-lg bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-ink transition-opacity hover:opacity-90"
              >
                {hasPatch ? 'Adopt and apply' : 'Adopt'}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void dismiss()}
                className="flex h-6.5 items-center rounded-lg border border-border px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
              >
                Dismiss
              </button>
            </div>
          )}
        </div>
      )}
    </li>
  )
}

function ReportRow({
  report: r,
  spaceId,
  isOwner,
  onChanged,
}: {
  report: FeedbackReport
  spaceId: string
  isOwner: boolean
  onChanged: () => void
}) {
  const { toast } = useToast()
  const [open, setOpen] = useState(false)
  const [publishing, setPublishing] = useState(false)

  const publish = async () => {
    setPublishing(true)
    try {
      await feedbackApi.publishReport(spaceId, r.id)
      toast('Report published', 'info')
      onChanged()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Publish failed', 'error')
    } finally {
      setPublishing(false)
    }
  }

  return (
    <li className="rounded-lg border border-border bg-surface">
      <div className="flex w-full items-center gap-2 px-3 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <IconChevron open={open} className="h-3.5 w-3.5 shrink-0 text-ink-3" />
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">
            {r.title}
          </span>
          <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
            {r.created_by} · {relativeTime(r.created_at)}
          </span>
        </button>
        {r.status === 'published' && r.doc_url ? (
          /* doc_url holds a server-side markdown file path — shown as a path string, not a link. */
          <span
            title={r.doc_url}
            className="max-w-[14rem] shrink-0 truncate font-mono text-[10.5px] text-ink-3"
          >
            {r.doc_url}
          </span>
        ) : isOwner ? (
          <button
            type="button"
            disabled={publishing}
            onClick={() => void publish()}
            className={cn(
              'flex h-6.5 shrink-0 items-center rounded-lg bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-ink transition-opacity hover:opacity-90',
              publishing && 'cursor-not-allowed opacity-50',
            )}
          >
            {publishing ? 'Publishing…' : 'Publish'}
          </button>
        ) : (
          <span className="shrink-0 text-[11px] text-ink-3">Draft</span>
        )}
      </div>
      {open && (
        <div className="border-t border-border px-3 py-2.5">
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words text-[12px] leading-relaxed text-ink-2">
            {r.body}
          </pre>
        </div>
      )}
    </li>
  )
}

function FeedbackRow({
  entry: fb,
  spaceId,
  onOpenSession,
  onChanged,
}: {
  entry: FeedbackEntry
  spaceId: string
  onOpenSession?: (sessionId: string) => void
  onChanged: () => void
}) {
  const { toast } = useToast()
  const [refOpen, setRefOpen] = useState(false)
  const [refText, setRefText] = useState('')
  const [busy, setBusy] = useState(false)

  const hasReference = fb.reference_kind !== 'none'

  const submitReference = async () => {
    const text = refText.trim()
    if (!text) return
    setBusy(true)
    try {
      await feedbackApi.putReference(spaceId, fb.id, { kind: 'text', text })
      toast('Reference submitted; the next analysis will re-attribute', 'info')
      setRefOpen(false)
      onChanged()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Failed to submit the reference', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="rounded-lg border border-border bg-surface px-3 py-2.5">
      <div className="flex items-center gap-2">
        {fb.rating === 1 ? (
          <IconThumbUp className="h-3.5 w-3.5 shrink-0 text-ink-3" />
        ) : (
          <IconThumbDown className="h-3.5 w-3.5 shrink-0 text-danger" />
        )}
        {fb.tags.map((t) => (
          <span
            key={t}
            className="shrink-0 rounded-full border border-border px-1.5 py-0.5 text-[10px] text-ink-2"
          >
            {t}
          </span>
        ))}
        {hasReference && (
          <span className="shrink-0 rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
            Has reference
          </span>
        )}
        {fb.rating === -1 && fb.analyzed_run_id && (
          <span className="shrink-0 text-[10.5px] text-ink-3">Analyzed</span>
        )}
        <span className="min-w-0 flex-1" />
        <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
          {fb.author} · {relativeTime(fb.created_at)}
        </span>
      </div>
      {fb.comment && (
        <p className="mt-1 text-[12.5px] leading-relaxed text-ink-2">
          {fb.comment}
        </p>
      )}
      <div className="mt-1.5 flex items-center gap-2.5 text-[11.5px]">
        {onOpenSession && (
          <button
            type="button"
            onClick={() => onOpenSession(fb.session_id)}
            className="text-accent hover:underline"
          >
            Open session
          </button>
        )}
        {fb.rating === -1 && !hasReference && (
          <button
            type="button"
            onClick={() => setRefOpen((v) => !v)}
            className="text-ink-3 hover:text-ink"
          >
            Add the correct result
          </button>
        )}
      </div>
      {refOpen && (
        <div className="mt-2 space-y-1.5 rounded-lg border border-border bg-bg p-2">
          <textarea
            value={refText}
            onChange={(e) => setRefText(e.target.value)}
            rows={3}
            placeholder="Paste the correct result text"
            className="w-full resize-y rounded-lg border border-border bg-surface px-2 py-1.5 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
          />
          <div className="flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setRefOpen(false)}
              className="rounded-lg px-2 py-1 text-[12px] text-ink-2 hover:bg-surface-2"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy || !refText.trim()}
              onClick={() => void submitReference()}
              className={cn(
                'rounded-lg bg-accent px-2.5 py-1 text-[12px] font-medium text-accent-ink hover:opacity-90',
                (busy || !refText.trim()) && 'cursor-not-allowed opacity-50',
              )}
            >
              {busy ? 'Submitting…' : 'Submit'}
            </button>
          </div>
        </div>
      )}
    </li>
  )
}
