import { useCallback, useEffect, useMemo, useState } from 'react'
import { boardApi, templatesApi } from '../api/endpoints'
import type {
  BoardCard,
  BoardCardLink,
  BoardColumnKey,
  Template,
} from '../api/types'
import { cn } from '../lib/cn'
import { useToast } from '../state/toast'
import { IconClose, IconPlus, IconRefresh, IconTrash } from './icons'
import { StartTemplateModal } from './workflow/StartTemplateModal'

/**
 * Task board (ADR-0016 Phase 2): three fixed columns (To do / Doing / Done).
 *
 * - Drag to change columns: dropping on a column lands at its end; dropping on a
 *   card inserts before that card (midpoint of the neighboring positions).
 * - Clicking a card opens the editor (title / description / assignee / due date /
 *   delete / backlink navigation).
 * - "Start from template": pick a space template → param form (reusing
 *   StartTemplateModal) → the backend creates a session and links it back to the card.
 * - No realtime push for concurrent edits: manual refresh / refetch after actions
 *   (the spec's acceptance criteria).
 */

const COLUMNS: { key: BoardColumnKey; label: string }[] = [
  { key: 'todo', label: 'To do' },
  { key: 'doing', label: 'Doing' },
  { key: 'done', label: 'Done' },
]

interface BoardPageProps {
  spaceId: string
  currentUser?: string
  isSpaceOwner: boolean
  /** Backlink navigation: topic → channel page with the topic panel auto-opened. */
  onOpenTopic: (channelId: string, topicId: string) => void
  /** Backlink navigation: session → session view. */
  onOpenSession: (sessionId: string) => void
}

export function BoardPage({
  spaceId,
  currentUser,
  isSpaceOwner,
  onOpenTopic,
  onOpenSession,
}: BoardPageProps) {
  const { toast } = useToast()
  const [cards, setCards] = useState<BoardCard[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<BoardCard | null>(null)
  const [creatingIn, setCreatingIn] = useState<BoardColumnKey | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)

  const reload = useCallback(() => {
    boardApi
      .list(spaceId)
      .then((r) => setCards(r.cards))
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load board'))
      .finally(() => setLoading(false))
  }, [spaceId, toast])

  useEffect(() => {
    setLoading(true)
    reload()
  }, [reload])

  const byColumn = useMemo(() => {
    const map: Record<BoardColumnKey, BoardCard[]> = {
      todo: [],
      doing: [],
      done: [],
    }
    for (const c of cards) {
      ;(map[c.column_key] ?? map.todo).push(c)
    }
    for (const key of Object.keys(map) as BoardColumnKey[]) {
      map[key].sort((a, b) => a.position - b.position)
    }
    return map
  }, [cards])

  /** Move a card (optimistic update + rollback-by-refetch on failure). Missing position = end of the target column. */
  const moveCard = useCallback(
    async (cardId: string, column: BoardColumnKey, position?: number) => {
      const target = cards.find((c) => c.id === cardId)
      if (!target) return
      const colCards = byColumn[column]
      const pos =
        position ??
        (colCards.length ? colCards[colCards.length - 1].position + 1 : 1)
      setCards((list) =>
        list.map((c) =>
          c.id === cardId ? { ...c, column_key: column, position: pos } : c,
        ),
      )
      try {
        await boardApi.updateCard(cardId, { column_key: column, position: pos })
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Move failed')
        reload()
      }
    },
    [cards, byColumn, reload, toast],
  )

  /** Drop on a card: insert before it (midpoint with the previous card in the column). */
  const dropOnCard = useCallback(
    (target: BoardCard) => {
      if (!dragId || dragId === target.id) return
      const colCards = byColumn[target.column_key]
      const idx = colCards.findIndex((c) => c.id === target.id)
      const prev = idx > 0 ? colCards[idx - 1] : null
      const pos = prev
        ? (prev.position + target.position) / 2
        : target.position - 1
      void moveCard(dragId, target.column_key, pos)
      setDragId(null)
    },
    [dragId, byColumn, moveCard],
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 px-4 pt-3 sm:px-6">
        <span className="text-[13px] text-ink-3">
          A task board shared by members; in channel topics the agent can also file
          and move cards.
        </span>
        <button
          type="button"
          title="Refresh"
          onClick={reload}
          className="ml-auto flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconRefresh className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-x-auto px-4 pb-4 pt-3 sm:px-6">
        <div className="flex h-full min-w-[720px] gap-3">
          {COLUMNS.map(({ key, label }) => (
            <div
              key={key}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                if (dragId) {
                  void moveCard(dragId, key)
                  setDragId(null)
                }
              }}
              className="flex min-h-0 w-1/3 min-w-[220px] flex-col rounded-xl border border-border bg-surface"
            >
              <div className="flex shrink-0 items-center gap-2 px-3 py-2.5">
                <span className="text-[12.5px] font-semibold text-ink">
                  {label}
                </span>
                <span className="font-mono text-[11px] text-ink-3">
                  {byColumn[key].length}
                </span>
                <button
                  type="button"
                  title={`New card in "${label}"`}
                  onClick={() => setCreatingIn(key)}
                  className="ml-auto flex h-5 w-5 items-center justify-center rounded text-ink-3 hover:bg-surface-2 hover:text-ink"
                >
                  <IconPlus className="h-3 w-3" />
                </button>
              </div>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2">
                {loading ? (
                  <div className="h-16 animate-pulse rounded-lg bg-surface-2" />
                ) : (
                  byColumn[key].map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      draggable
                      onDragStart={() => setDragId(c.id)}
                      onDragEnd={() => setDragId(null)}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        e.preventDefault()
                        e.stopPropagation()
                        dropOnCard(c)
                      }}
                      onClick={() => setEditing(c)}
                      className={cn(
                        'block w-full cursor-grab rounded-lg border border-border bg-bg px-3 py-2.5 text-left transition-colors hover:border-border-strong',
                        dragId === c.id && 'opacity-50',
                      )}
                    >
                      <p className="text-[13px] font-medium leading-snug text-ink">
                        {c.title}
                      </p>
                      {(c.assignee || c.due_date) && (
                        <p className="mt-1 flex flex-wrap gap-x-2 font-mono text-[10.5px] text-ink-3">
                          {c.assignee && <span>@{c.assignee}</span>}
                          {c.due_date && <span>due {c.due_date}</span>}
                        </p>
                      )}
                      {c.links.length > 0 && (
                        <p className="mt-1 flex flex-wrap gap-1">
                          {c.links.map((l) => (
                            <span
                              key={`${l.type}:${l.id}`}
                              className="rounded border border-border px-1 py-0.5 font-mono text-[10px] text-ink-3"
                            >
                              {l.type === 'topic' ? 'Topic' : 'Session'}
                            </span>
                          ))}
                        </p>
                      )}
                    </button>
                  ))
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {creatingIn && (
        <CreateCardModal
          column={creatingIn}
          onClose={() => setCreatingIn(null)}
          onCreate={async (title) => {
            try {
              const r = await boardApi.createCard(spaceId, {
                title,
                column_key: creatingIn,
              })
              setCards((list) => [...list, r.card])
              setCreatingIn(null)
            } catch (e) {
              toast(e instanceof Error ? e.message : 'Create failed')
            }
          }}
        />
      )}

      {editing && (
        <CardModal
          spaceId={spaceId}
          card={editing}
          canDelete={isSpaceOwner || editing.created_by === currentUser}
          onClose={() => setEditing(null)}
          onChanged={(card) => {
            setCards((list) =>
              card
                ? list.map((c) => (c.id === card.id ? card : c))
                : list.filter((c) => c.id !== editing.id),
            )
            if (!card) setEditing(null)
          }}
          onOpenTopic={onOpenTopic}
          onOpenSession={onOpenSession}
        />
      )}
    </div>
  )
}

function CreateCardModal({
  column,
  onClose,
  onCreate,
}: {
  column: BoardColumnKey
  onClose: () => void
  onCreate: (title: string) => Promise<void>
}) {
  const [title, setTitle] = useState('')
  const label = COLUMNS.find((c) => c.key === column)?.label ?? column
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-sm rounded-xl border border-border bg-surface p-4 shadow-xl">
        <p className="mb-2 text-[13.5px] font-semibold text-ink">
          New card · {label}
        </p>
        <input
          autoFocus
          value={title}
          placeholder="Card title"
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && title.trim() && !e.nativeEvent.isComposing) {
              void onCreate(title.trim())
            } else if (e.key === 'Escape') {
              onClose()
            }
          }}
          className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink outline-none focus:border-border-strong"
        />
        <div className="mt-3 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-[12.5px] text-ink-2 hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!title.trim()}
            onClick={() => void onCreate(title.trim())}
            className="rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-white disabled:opacity-40"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  )
}

/** Card editor overlay: field editing + backlink navigation + start an execution session from a template + delete. */
function CardModal({
  spaceId,
  card,
  canDelete,
  onClose,
  onChanged,
  onOpenTopic,
  onOpenSession,
}: {
  spaceId: string
  card: BoardCard
  canDelete: boolean
  onClose: () => void
  onChanged: (card: BoardCard | null) => void
  onOpenTopic: (channelId: string, topicId: string) => void
  onOpenSession: (sessionId: string) => void
}) {
  const { toast } = useToast()
  const [title, setTitle] = useState(card.title)
  const [description, setDescription] = useState(card.description)
  const [assignee, setAssignee] = useState(card.assignee ?? '')
  const [dueDate, setDueDate] = useState(card.due_date ?? '')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateId, setTemplateId] = useState('')
  const [paramsFor, setParamsFor] = useState<Template | null>(null)

  useEffect(() => {
    templatesApi
      .list(spaceId)
      .then((r) => setTemplates(r.templates))
      .catch(() => setTemplates([]))
  }, [spaceId])

  const save = async () => {
    try {
      const r = await boardApi.updateCard(card.id, {
        title: title.trim() || card.title,
        description,
        assignee: assignee.trim() || undefined,
        clear_assignee: !assignee.trim(),
        due_date: dueDate.trim() || undefined,
        clear_due_date: !dueDate.trim(),
      })
      onChanged(r.card)
      onClose()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed')
    }
  }

  const remove = async () => {
    try {
      await boardApi.removeCard(card.id)
      onChanged(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  const startFromTemplate = async (values: Record<string, string>) => {
    if (!paramsFor) return
    try {
      const r = await boardApi.startSession(card.id, paramsFor.id, values)
      onChanged(r.card)
      setParamsFor(null)
      toast('Execution session started; the link is attached to the card', 'info')
      onOpenSession(r.session.id)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to start')
      throw e
    }
  }

  const openLink = (l: BoardCardLink) => {
    if (l.type === 'topic' && l.channel_id) onOpenTopic(l.channel_id, l.id)
    else if (l.type === 'session') onOpenSession(l.id)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-y-auto rounded-xl border border-border bg-surface p-4 shadow-xl">
        <div className="flex items-center gap-2">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-[15px] font-semibold text-ink outline-none hover:border-border focus:border-border-strong"
          />
          <button
            type="button"
            title="Close"
            onClick={onClose}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose />
          </button>
        </div>

        <textarea
          value={description}
          placeholder="Description (optional)"
          rows={4}
          onChange={(e) => setDescription(e.target.value)}
          className="mt-3 w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-[13px] leading-relaxed text-ink outline-none placeholder:text-ink-3 focus:border-border-strong"
        />

        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-ink-3">Assignee</span>
            <input
              value={assignee}
              placeholder="username"
              onChange={(e) => setAssignee(e.target.value)}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-border-strong"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-ink-3">Due date</span>
            <input
              type="date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-border-strong"
            />
          </label>
        </div>

        {card.links.length > 0 && (
          <div className="mt-3">
            <span className="mb-1 block text-[11.5px] text-ink-3">Links</span>
            <div className="flex flex-wrap gap-1.5">
              {card.links.map((l) => (
                <button
                  key={`${l.type}:${l.id}`}
                  type="button"
                  onClick={() => openLink(l)}
                  className="flex items-center gap-1 rounded-lg border border-border bg-bg px-2 py-1 text-[12px] text-ink transition-colors hover:border-border-strong hover:bg-surface-2"
                >
                  <span className="font-mono text-[10px] text-ink-3">
                    {l.type === 'topic' ? 'Topic' : 'Session'}
                  </span>
                  <span className="max-w-[14rem] truncate">{l.label}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {templates.length > 0 && (
          <div className="mt-3">
            <span className="mb-1 block text-[11.5px] text-ink-3">
              Hand off to the agent (start a session from a template; the link
              attaches back to the card)
            </span>
            <div className="flex gap-2">
              <select
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
                className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-border-strong"
              >
                <option value="">Select a template…</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={!templateId}
                onClick={() => {
                  const tpl = templates.find((t) => t.id === templateId)
                  if (tpl) setParamsFor(tpl)
                }}
                className="shrink-0 rounded-lg border border-border bg-bg px-3 py-1.5 text-[12.5px] text-ink transition-colors hover:border-border-strong hover:bg-surface-2 disabled:opacity-40"
              >
                Start
              </button>
            </div>
          </div>
        )}

        <div className="mt-4 flex items-center gap-2">
          {canDelete &&
            (confirmDelete ? (
              <button
                type="button"
                onClick={() => void remove()}
                onBlur={() => setConfirmDelete(false)}
                autoFocus
                className="rounded-lg bg-danger px-3 py-1.5 text-[12.5px] font-medium text-white"
              >
                Confirm delete
              </button>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-[12.5px] text-ink-3 hover:bg-surface-2 hover:text-danger"
              >
                <IconTrash className="h-3.5 w-3.5" />
                Delete
              </button>
            ))}
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-[12.5px] text-ink-2 hover:bg-surface-2"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void save()}
              className="rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-white"
            >
              Save
            </button>
          </div>
        </div>
      </div>

      {paramsFor && (
        <StartTemplateModal
          title={paramsFor.name}
          description={paramsFor.description}
          prompt={paramsFor.prompt}
          params={paramsFor.params}
          onSubmit={startFromTemplate}
          onClose={() => setParamsFor(null)}
        />
      )}
    </div>
  )
}
