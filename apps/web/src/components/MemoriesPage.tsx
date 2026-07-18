import { useCallback, useEffect, useState } from 'react'
import { memoriesApi } from '../api/endpoints'
import type { MemoryEntry } from '../api/types'
import { cn } from '../lib/cn'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import { IconChevron, IconSearch, IconTrash } from './icons'

/** frontmatter type → list badge label (legacy files may carry an empty string → no badge). */
const TYPE_LABELS: Record<string, string> = {
  user: 'User',
  project: 'Project',
  procedural: 'Procedural',
  reference: 'Reference',
}

/**
 * Memories page (layout matches KnowledgePage: left-aligned header + toolbar + row
 * list). The agent's long-term memory is isolated per space (personal space =
 * private memory, team space = shared among members); clicking a row opens the
 * editor (full text lazily loaded). Members can edit / archive; physical delete is
 * owner only.
 */
export function MemoriesPage() {
  const { currentSpaceId, currentSpace } = useSpace()
  const { toast } = useToast()
  const [memories, setMemories] = useState<MemoryEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [openName, setOpenName] = useState<string | null>(null)

  const isOwner = currentSpace?.my_role === 'owner'

  const refresh = useCallback(async () => {
    if (!currentSpaceId) return
    setLoading(true)
    try {
      const { memories } = await memoriesApi.list(currentSpaceId)
      setMemories(memories)
    } catch {
      toast('Failed to load memories', 'error')
    } finally {
      setLoading(false)
    }
  }, [currentSpaceId, toast])

  useEffect(() => {
    setOpenName(null)
    void refresh()
  }, [refresh])

  const q = query.trim().toLowerCase()
  const visible = q
    ? memories.filter(
        (m) =>
          m.name.toLowerCase().includes(q) ||
          m.description.toLowerCase().includes(q),
      )
    : memories

  return (
    <div className="flex h-full w-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-8">
          <h1 className="text-[20px] font-semibold text-ink">Memories</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
            Long-term memory the agent records on its own during conversations —
            effective across sessions, isolated per space. Members can view and edit;
            prefer archiving outdated memories over deleting them.
          </p>

          {/* Toolbar: count on the left / search on the right */}
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="font-mono text-[11.5px] text-ink-3">
              {loading
                ? 'Loading…'
                : q
                  ? `${visible.length} / ${memories.length} memor${memories.length === 1 ? 'y' : 'ies'}`
                  : `${memories.length} memor${memories.length === 1 ? 'y' : 'ies'}`}
            </span>
            <div className="flex h-7 items-center gap-1.5 rounded-lg border border-border bg-bg px-2.5">
              <IconSearch className="h-3.5 w-3.5 shrink-0 text-ink-3" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search memories…"
                className="w-44 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
              />
            </div>
          </div>

          {/* List */}
          <div className="mt-4">
            {loading ? (
              <div className="space-y-2">
                {[0, 1].map((i) => (
                  <div key={i} className="h-14 animate-pulse rounded-lg bg-surface-2" />
                ))}
              </div>
            ) : memories.length === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No memories yet. When a conversation surfaces something worth keeping
                across sessions, the agent records it automatically.
              </p>
            ) : visible.length === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No memories match "{query}".
              </p>
            ) : (
              <ul className="space-y-2">
                {visible.map((m) => (
                  <MemoryRow
                    key={m.name}
                    entry={m}
                    spaceId={currentSpaceId!}
                    open={openName === m.name}
                    isOwner={isOwner}
                    onToggle={() =>
                      setOpenName((cur) => (cur === m.name ? null : m.name))
                    }
                    onChanged={() => void refresh()}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function MemoryRow({
  entry,
  spaceId,
  open,
  isOwner,
  onToggle,
  onChanged,
}: {
  entry: MemoryEntry
  spaceId: string
  open: boolean
  isOwner: boolean
  onToggle: () => void
  onChanged: () => void
}) {
  const { toast } = useToast()
  // null = not loaded yet (the full text is lazily fetched once on expand).
  const [text, setText] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open || text !== null) return
    memoriesApi
      .get(spaceId, entry.name)
      .then((r) => setText(r.text))
      .catch(() => toast('Failed to load memory', 'error'))
  }, [open, text, spaceId, entry.name, toast])

  const save = async () => {
    if (!text?.trim()) return
    setSaving(true)
    try {
      await memoriesApi.write(spaceId, entry.name, text)
      toast('Saved', 'info')
      onChanged()
    } catch {
      toast('Save failed', 'error')
    } finally {
      setSaving(false)
    }
  }

  const archive = async () => {
    try {
      await memoriesApi.archive(spaceId, entry.name)
      toast('Archived', 'info')
      onChanged()
    } catch {
      toast('Archive failed', 'error')
    }
  }

  const remove = async () => {
    if (!window.confirm(`Permanently delete memory "${entry.name}"? Archiving keeps it traceable.`)) return
    try {
      await memoriesApi.remove(spaceId, entry.name)
      toast('Deleted', 'info')
      onChanged()
    } catch {
      toast('Delete failed', 'error')
    }
  }

  const typeLabel = TYPE_LABELS[entry.type] ?? (entry.type || null)

  return (
    <li className="rounded-lg border border-border bg-surface">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left"
      >
        <IconChevron
          open={open}
          className="h-3.5 w-3.5 shrink-0 text-ink-3"
        />
        <span className="min-w-0 shrink-0 font-mono text-[12px] text-ink">
          {entry.name}
        </span>
        {typeLabel && (
          <span className="shrink-0 rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
            {typeLabel}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-3">
          {entry.description}
        </span>
        {entry.updated_at != null && (
          <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
            {new Date(entry.updated_at * 1000).toLocaleDateString()}
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-border px-3 py-2.5">
          {text === null ? (
            <div className="h-24 animate-pulse rounded-lg bg-surface-2" />
          ) : (
            <>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={Math.min(16, Math.max(6, text.split('\n').length + 1))}
                spellCheck={false}
                className="w-full resize-y rounded-lg border border-border bg-bg px-2.5 py-2 font-mono text-[12px] leading-relaxed text-ink outline-none focus:border-accent"
              />
              <div className="mt-2 flex items-center justify-end gap-1.5">
                {isOwner && (
                  <button
                    type="button"
                    onClick={() => void remove()}
                    className="mr-auto flex h-7 items-center gap-1 rounded-lg px-2.5 text-[12px] text-danger transition-colors hover:bg-surface-2"
                  >
                    <IconTrash className="h-3.5 w-3.5" />
                    Delete
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void archive()}
                  className="flex h-7 items-center rounded-lg border border-border px-2.5 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
                >
                  Archive
                </button>
                <button
                  type="button"
                  onClick={() => void save()}
                  disabled={saving || !text.trim()}
                  className={cn(
                    'flex h-7 items-center rounded-lg bg-accent px-2.5 text-[12px] font-medium text-accent-ink transition-opacity hover:opacity-90',
                    (saving || !text.trim()) && 'cursor-not-allowed opacity-50',
                  )}
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </li>
  )
}
