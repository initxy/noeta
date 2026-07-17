import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import type { ModelInfo } from '../api/types'
import { cn } from '../lib/cn'
import { sendKeyHint, useSendMode } from '../state/sendMode'
import { IconCheck, IconChevron, IconSend, IconStop } from './icons'

/** Effort display labels: low/medium/high/xhigh/max → Low/Medium/High/Extra high/Max; anything else as-is. */
const EFFORT_LABELS: Record<string, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max',
}
const effortLabel = (e: string) => EFFORT_LABELS[e] ?? e

/** Compact model name on the trigger: strip a GPT prefix, e.g. "GPT-5.5" → "5.5"; unchanged without one. */
const shortModelLabel = (label: string) =>
  label.replace(/^gpt[\s-]*/i, '').trim() || label

interface ComposerProps {
  onSend: (content: string) => Promise<void>
  onStop: () => void
  running: boolean
  disabled: boolean
  models: ModelInfo[]
  model: string
  onModelChange: (id: string) => void
  effort: string
  onEffortChange: (e: string) => void
  /** Enlarged presentation in the hero empty state */
  hero?: boolean
}

export function Composer({
  onSend,
  onStop,
  running,
  disabled,
  models,
  model,
  onModelChange,
  effort,
  onEffortChange,
  hero = false,
}: ComposerProps) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // Enter during IME composition only confirms the candidate, never sends; a
  // self-maintained flag backstops isComposing timing differences.
  const composingRef = useRef(false)
  const [sendMode] = useSendMode()

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`
  }, [value])

  const canSend = !disabled && !running && !busy && value.trim().length > 0

  const submit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (!canSend) return
    const content = value.trim()
    setBusy(true)
    try {
      await onSend(content)
      setValue('')
    } finally {
      setBusy(false)
      taRef.current?.focus()
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return
    // Enter while composing with an IME must never send: isComposing flips to
    // false before compositionend in some browsers, so composingRef and
    // keyCode===229 backstop it.
    if (
      e.nativeEvent.isComposing ||
      composingRef.current ||
      e.nativeEvent.keyCode === 229
    ) {
      return
    }
    // enter mode: Enter sends, Shift+Enter newline; mod-enter mode: ⌘/Ctrl+Enter sends, Enter newline
    const wantSend =
      sendMode === 'enter' ? !e.shiftKey : e.metaKey || e.ctrlKey
    if (!wantSend) return // Leave the newline to the textarea default behavior
    e.preventDefault()
    void submit()
  }

  const currentModel = models.find((m) => m.id === model)
  const currentEfforts = currentModel?.efforts ?? []
  const triggerModel = currentModel
    ? shortModelLabel(currentModel.label || currentModel.id)
    : 'Default model'

  // Picking a model switches it (the parent resets the effort as needed).
  // Models without efforts need no further pick — close directly.
  const onPickModel = (m: ModelInfo) => {
    if (m.id !== model) onModelChange(m.id)
    if (!m.efforts || m.efforts.length === 0) setMenuOpen(false)
  }
  const onPickEffort = (e: string) => {
    onEffortChange(e)
    setMenuOpen(false)
  }

  return (
    <form
      onSubmit={submit}
      className={cn(
        'rounded-2xl border bg-surface shadow-[var(--shadow)] transition-colors focus-within:border-accent',
        hero ? 'border-border-strong' : 'border-border',
      )}
    >
      <textarea
        ref={taRef}
        rows={hero ? 3 : 1}
        value={value}
        disabled={disabled}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onCompositionStart={() => {
          composingRef.current = true
        }}
        onCompositionEnd={() => {
          composingRef.current = false
        }}
        placeholder={
          running
            ? 'The agent is working — stop it before sending a new message…'
            : 'Describe your task…'
        }
        className="block max-h-48 w-full resize-none bg-transparent px-4 pt-3.5 text-[14.5px] leading-relaxed text-ink outline-none placeholder:text-ink-3 disabled:opacity-50"
      />
      <div className="flex items-center justify-between px-2.5 pb-2.5 pt-1.5">
        <div className="relative flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            disabled={disabled || models.length === 0}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title="Choose model and reasoning effort"
            className={cn(
              'flex items-center gap-1 rounded-md border px-2 py-1 text-[12px] transition-colors',
              menuOpen
                ? 'border-border bg-surface-2'
                : 'border-transparent hover:border-border hover:bg-surface-2',
              'disabled:cursor-default disabled:opacity-50 disabled:hover:border-transparent disabled:hover:bg-transparent',
            )}
          >
            <span className="max-w-40 truncate font-medium text-ink-2">
              {triggerModel}
            </span>
            {effort && <span className="text-ink-3">{effortLabel(effort)}</span>}
            <IconChevron className="h-3.5 w-3.5 text-ink-3" open={menuOpen} />
          </button>

          {menuOpen && (
            <>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setMenuOpen(false)}
                className="fixed inset-0 z-30"
              />
              <div className="absolute bottom-full left-0 z-40 mb-1.5 min-w-52 overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-[var(--shadow)]">
                <div className="px-3 pb-1 pt-1 text-[10px] font-medium uppercase tracking-wide text-ink-3">
                  Model
                </div>
                <ul>
                  {models.map((m) => (
                    <li key={m.id}>
                      <button
                        type="button"
                        onClick={() => onPickModel(m)}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] transition-colors hover:bg-surface-2"
                      >
                        <span className="min-w-0 flex-1 truncate text-ink">
                          {m.label || m.id}
                        </span>
                        {m.id === model && (
                          <IconCheck className="h-3.5 w-3.5 shrink-0 text-accent" />
                        )}
                      </button>
                    </li>
                  ))}
                </ul>

                {currentEfforts.length > 0 && (
                  <>
                    <div className="my-1 border-t border-border" />
                    <div className="px-3 pb-1 pt-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-3">
                      Reasoning
                    </div>
                    <ul>
                      {currentEfforts.map((e) => (
                        <li key={e}>
                          <button
                            type="button"
                            onClick={() => onPickEffort(e)}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] transition-colors hover:bg-surface-2"
                          >
                            <span className="min-w-0 flex-1 truncate text-ink">
                              {effortLabel(e)}
                            </span>
                            {e === effort && (
                              <IconCheck className="h-3.5 w-3.5 shrink-0 text-accent" />
                            )}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </>
          )}
        </div>

        {running ? (
          <button
            type="button"
            onClick={onStop}
            title="Stop this turn"
            className="flex h-8 items-center gap-1.5 rounded-lg border border-border px-3 text-[12.5px] text-ink-2 transition-colors hover:border-danger/50 hover:text-danger"
          >
            <IconStop className="h-3.5 w-3.5" />
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSend}
            title={`Send (${sendKeyHint(sendMode)})`}
            className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-accent-ink transition-opacity hover:opacity-90 disabled:opacity-30"
          >
            <IconSend className="h-4 w-4" />
          </button>
        )}
      </div>
    </form>
  )
}
