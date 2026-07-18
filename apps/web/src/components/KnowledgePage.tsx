import { useCallback, useEffect, useRef, useState } from 'react'
import { knowledgeApi } from '../api/endpoints'
import type { KnowledgeSource, SyncProgress } from '../api/types'
import { cn } from '../lib/cn'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import {
  IconClose,
  IconFolder,
  IconGit,
  IconPlus,
  IconSearch,
  IconSync,
  IconTrash,
} from './icons'
import { SyncProgressBar } from './SyncProgressBar'

/**
 * Knowledge-base configuration page: header + toolbar (count / search / add source)
 * + row list. List / 2s polling of syncing sources / sync trigger / delete. "Add
 * knowledge source" opens a right-hand slide-over form.
 */
export function KnowledgePage() {
  const { currentSpaceId, currentSpace } = useSpace()
  const { toast } = useToast()
  const [sources, setSources] = useState<KnowledgeSource[]>([])
  const [loading, setLoading] = useState(true)
  // The type is chosen before the drawer opens: null = closed, otherwise the type to add.
  const [createType, setCreateType] = useState<'git_repo' | 'local_dir' | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const addMenuRef = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState('')
  const [pollingIds, setPollingIds] = useState<Set<string>>(new Set())
  const [progressMap, setProgressMap] = useState<Map<string, SyncProgress | null>>(new Map())

  const isOwner = currentSpace?.my_role === 'owner'

  // Progress of syncing sources: fetch sync status one by one (low frequency;
  // usually only one source syncs at a time).
  const refreshProgress = useCallback(
    async (ids: string[]) => {
      if (!currentSpaceId || ids.length === 0) return
      const entries = await Promise.all(
        ids.map(async (id) => {
          try {
            const st = await knowledgeApi.syncStatus(currentSpaceId, id)
            return [id, st.progress] as const
          } catch {
            return [id, null] as const
          }
        }),
      )
      setProgressMap((prev) => {
        const next = new Map(prev)
        for (const [id, p] of entries) next.set(id, p)
        return next
      })
    },
    [currentSpaceId],
  )

  const load = useCallback(async () => {
    if (!currentSpaceId) return
    setLoading(true)
    try {
      const r = await knowledgeApi.list(currentSpaceId)
      setSources(r.sources)
      // Start polling sources that are syncing.
      const syncing = r.sources.filter((s) => s.status === 'syncing')
      if (syncing.length > 0) {
        setPollingIds((prev) => {
          const next = new Set(prev)
          syncing.forEach((s) => next.add(s.id))
          return next
        })
        void refreshProgress(syncing.map((s) => s.id))
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load knowledge sources')
    } finally {
      setLoading(false)
    }
  }, [currentSpaceId, toast, refreshProgress])

  useEffect(() => {
    void load()
  }, [load])

  // "Add knowledge source" dropdown: close on outside click.
  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => {
      if (addMenuRef.current && !addMenuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

  // Poll sync status + progress.
  useEffect(() => {
    if (pollingIds.size === 0 || !currentSpaceId) return
    const timer = setInterval(async () => {
      try {
        const r = await knowledgeApi.list(currentSpaceId)
        setSources(r.sources)
        const stillSyncing = r.sources
          .filter((s) => s.status === 'syncing')
          .map((s) => s.id)
        setPollingIds(new Set(stillSyncing))
        if (stillSyncing.length > 0) void refreshProgress(stillSyncing)
        else setProgressMap(new Map())
      } catch {
        /* Silent; try again next tick. */
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [pollingIds.size, currentSpaceId, refreshProgress])

  const triggerSync = async (sourceId: string) => {
    if (!currentSpaceId) return
    try {
      await knowledgeApi.sync(currentSpaceId, sourceId)
      setPollingIds((prev) => new Set(prev).add(sourceId))
      toast('Sync started', 'info')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Sync failed')
    }
  }

  const removeSource = async (sourceId: string) => {
    if (!currentSpaceId) return
    try {
      await knowledgeApi.remove(currentSpaceId, sourceId)
      toast('Deleted', 'info')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  const q = query.trim().toLowerCase()
  const visible = q
    ? sources.filter((s) => s.name.toLowerCase().includes(q))
    : sources

  return (
    <div className="flex h-full w-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-8">
          {/* Header (left-aligned, sharing the toolbar/list left edge so a centered
              header doesn't misalign with the full-width toolbar). */}
          <h1 className="text-[20px] font-semibold text-ink">Knowledge base</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
            Configure git repositories or local directories as space knowledge sources;
            once synced, sessions of every space member can search them.
          </p>

          {/* Non-owner notice bar */}
          {!isOwner && (
            <p className="mt-4 rounded-lg bg-surface-2 px-3 py-2 text-center text-[12px] text-ink-3">
              Only space owners can manage knowledge sources; members can view sync status.
            </p>
          )}

          {/* Toolbar: count on the left / search + add source on the right */}
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="font-mono text-[11.5px] text-ink-3">
              {loading
                ? 'Loading…'
                : q
                  ? `${visible.length} / ${sources.length} source${sources.length === 1 ? '' : 's'}`
                  : `${sources.length} source${sources.length === 1 ? '' : 's'}`}
            </span>
            <div className="flex items-center gap-1.5">
              <div className="flex h-7 items-center gap-1.5 rounded-lg border border-border bg-bg px-2.5">
                <IconSearch className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search sources…"
                  className="w-44 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
                />
              </div>
              {isOwner && (
                <div ref={addMenuRef} className="relative">
                  <button
                    type="button"
                    onClick={() => setMenuOpen((v) => !v)}
                    className="flex h-7 items-center gap-1 rounded-lg bg-accent px-2.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90"
                  >
                    <IconPlus className="h-3.5 w-3.5" />
                    Add source
                  </button>
                  {menuOpen && (
                    <div className="absolute right-0 z-20 mt-1 w-44 overflow-hidden rounded-lg border border-border bg-surface py-1 shadow-[var(--shadow)]">
                      <button
                        type="button"
                        onClick={() => {
                          setMenuOpen(false)
                          setCreateType('git_repo')
                        }}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-ink hover:bg-surface-2"
                      >
                        <IconGit className="h-3.5 w-3.5 text-ink-3" />
                        Git repository
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setMenuOpen(false)
                          setCreateType('local_dir')
                        }}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-ink hover:bg-surface-2"
                      >
                        <IconFolder className="h-3.5 w-3.5 text-ink-3" />
                        Local directory
                      </button>
                    </div>
                  )}
                </div>
              )}
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
            ) : sources.length === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No knowledge sources yet. Add a git repository or a local directory;
                after a sync the agent can search it.
              </p>
            ) : visible.length === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No sources match "{query}".
              </p>
            ) : (
              <ul className="space-y-2">
                {visible.map((s) => (
                  <SourceItem
                    key={s.id}
                    source={s}
                    isOwner={!!isOwner}
                    progress={progressMap.get(s.id) ?? null}
                    onSync={() => void triggerSync(s.id)}
                    onDelete={() => void removeSource(s.id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      {/* Slide-over create panel: key bound to currentSpaceId so a space switch
          remounts it — space A's form content is never submitted to space B. */}
      {createType && currentSpaceId && (
        <CreateSourceSlideOver
          key={currentSpaceId}
          spaceId={currentSpaceId}
          type={createType}
          onClose={() => setCreateType(null)}
          onCreated={(sourceId) => {
            setCreateType(null)
            setPollingIds((prev) => new Set(prev).add(sourceId))
            void load()
          }}
        />
      )}
    </div>
  )
}

/** Safely read a non-empty string from config (Record<string, unknown>); null when absent. */
function readConfigStr(config: Record<string, unknown>, key: string): string | null {
  const v = config[key]
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

/** Git URL → readable repository label: strip the protocol prefix and trailing .git. */
function repoLabelFromGitUrl(gitUrl: string): string {
  return gitUrl.replace(/^https?:\/\//, '').replace(/\.git$/, '')
}

function SourceItem({
  source,
  isOwner,
  progress,
  onSync,
  onDelete,
}: {
  source: KnowledgeSource
  isOwner: boolean
  progress: SyncProgress | null
  onSync: () => void
  onDelete: () => void
}) {
  const statusLabel: Record<string, string> = {
    pending: 'Pending',
    syncing: 'Syncing',
    ready: 'Ready',
    failed: 'Failed',
  }
  const statusColor: Record<string, string> = {
    pending: 'bg-surface-2 text-ink-3',
    syncing: 'bg-accent-soft text-accent',
    ready: 'bg-accent-soft text-accent',
    failed: 'bg-danger-soft text-danger',
  }

  const typeIcon =
    source.type === 'git_repo' ? (
      <IconGit className="h-4 w-4 text-ink-3" />
    ) : (
      <IconFolder className="h-4 w-4 text-ink-3" />
    )
  const typeLabel = source.type === 'git_repo' ? 'Git repository' : 'Local directory'

  // Source info: where it points to (shown for every status). git_repo → repo label
  // + branch; local_dir → path.
  const gitUrl = source.type === 'git_repo' ? readConfigStr(source.config, 'url') : null
  const repoLabel = gitUrl ? repoLabelFromGitUrl(gitUrl) : null
  const branch = source.type === 'git_repo' ? readConfigStr(source.config, 'branch') : null
  const dirPath = source.type === 'local_dir' ? readConfigStr(source.config, 'path') : null

  return (
    <li className="rounded-lg border border-border bg-bg px-3 py-2.5">
      <div className="flex items-center gap-2">
        {typeIcon}
        <span className="truncate text-[13px] font-medium text-ink">{source.name}</span>
        <span
          className={cn(
            'shrink-0 rounded-md px-1.5 py-0.5 text-[10.5px]',
            statusColor[source.status] || statusColor.pending,
          )}
        >
          {source.status === 'syncing' ? (
            <span className="flex items-center gap-1">
              <IconSync className="h-3 w-3 animate-spin" />
              Syncing
            </span>
          ) : (
            statusLabel[source.status] || source.status
          )}
        </span>
        <span className="shrink-0 text-[11px] text-ink-3">{typeLabel}</span>
        <div className="flex-1" />
        {isOwner && (
          <>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onSync()
              }}
              disabled={source.status === 'syncing'}
              title="Sync"
              className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-40"
            >
              <IconSync className={cn('h-3.5 w-3.5', source.status === 'syncing' && 'animate-spin')} />
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onDelete()
              }}
              title="Delete"
              className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-danger"
            >
              <IconTrash className="h-3.5 w-3.5" />
            </button>
          </>
        )}
      </div>
      {source.status === 'syncing' && (
        <div className="mt-2">
          <SyncProgressBar progress={progress} compact />
        </div>
      )}
      {source.last_error && source.status === 'failed' && (
        <p className="mt-1 truncate text-[11.5px] text-danger">{source.last_error}</p>
      )}
      {(repoLabel || branch) && (
        <p
          className="mt-0.5 truncate text-[11px] text-ink-3"
          title={repoLabel ?? undefined}
        >
          {repoLabel}
          {repoLabel && branch && ' · '}
          {branch && `branch ${branch}`}
        </p>
      )}
      {dirPath && (
        <p className="mt-0.5 truncate font-mono text-[11px] text-ink-3" title={dirPath}>
          {dirPath}
        </p>
      )}
      {source.status === 'ready' &&
        (source.last_sync_at || source.doc_count != null || !!source.failed_count) && (
          <p className="mt-0.5 text-[11px] text-ink-3">
            {source.doc_count != null &&
              `${source.doc_count} document${source.doc_count === 1 ? '' : 's'}`}
            {source.doc_count != null && source.last_sync_at && ' · '}
            {source.last_sync_at &&
              `last synced ${new Date(source.last_sync_at * 1000).toLocaleString()}`}
            {!!source.failed_count && source.failed_count > 0 && (
              <span className="text-danger">
                {(source.doc_count != null || source.last_sync_at) && ' · '}
                {source.failed_count} failed to sync
              </span>
            )}
          </p>
        )}
    </li>
  )
}

// ---------------------------------------------------------- Slide-over create panel

/** Right-hand slide-over: the type comes fixed from the parent (no switching inside
 * the drawer). git_repo: url + optional branch + optional token; local_dir: path. */
function CreateSourceSlideOver({
  spaceId,
  type,
  onClose,
  onCreated,
}: {
  spaceId: string
  type: 'git_repo' | 'local_dir'
  onClose: () => void
  onCreated: (sourceId: string) => void
}) {
  const { toast } = useToast()
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [branch, setBranch] = useState('')
  const [token, setToken] = useState('')
  const [path, setPath] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    if (submitting) return
    const trimmedName = name.trim()
    if (!trimmedName) {
      toast('Please enter a name')
      return
    }
    setSubmitting(true)
    try {
      let config: Record<string, unknown>
      if (type === 'git_repo') {
        if (!url.trim()) {
          toast('Please enter the repository URL')
          setSubmitting(false)
          return
        }
        config = { url: url.trim() }
        if (branch.trim()) config.branch = branch.trim()
        if (token.trim()) config.token = token.trim()
      } else {
        if (!path.trim()) {
          toast('Please enter the directory path')
          setSubmitting(false)
          return
        }
        config = { path: path.trim() }
      }
      const created = await knowledgeApi.create(spaceId, {
        name: trimmedName,
        type,
        config,
      })
      try {
        await knowledgeApi.sync(spaceId, created.source.id)
      } catch {
        /* Created but the sync trigger failed: the user can sync manually from the list. */
      }
      toast('Created; syncing', 'info')
      onCreated(created.source.id)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Create failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50">
      {/* Translucent backdrop on the left; click to close. */}
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/40"
      />
      <div className="slide-over-enter absolute inset-y-0 right-0 flex w-[640px] max-w-full flex-col border-l border-border bg-surface shadow-[var(--shadow)]">
        {/* Top: title + close button */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-[15px] font-semibold text-ink">
            {type === 'git_repo' ? 'Add git repository' : 'Add local directory'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>

        {/* Scrollable content */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <label className="mb-1 block text-[12px] text-ink-2">Name</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Product docs"
            maxLength={128}
            className="mb-3 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 text-[13px] text-ink focus:border-border-strong focus:outline-none"
          />

          {type === 'git_repo' ? (
            <>
              <label className="mb-1 block text-[12px] text-ink-2">Repository URL</label>
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://github.com/org/repo.git"
                className="mb-3 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              />
              <label className="mb-1 block text-[12px] text-ink-2">
                Branch (optional; the default branch when empty)
              </label>
              <input
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
                placeholder="main"
                className="mb-3 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              />
              <label className="mb-1 block text-[12px] text-ink-2">
                Access token (optional; for private repositories)
              </label>
              <input
                value={token}
                onChange={(e) => setToken(e.target.value)}
                type="password"
                autoComplete="off"
                placeholder="Personal access token"
                className="mb-3 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              />
            </>
          ) : (
            <>
              <label className="mb-1 block text-[12px] text-ink-2">Directory path</label>
              <input
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/data/docs"
                className="mb-3 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              />
              <p className="mb-3 text-[11px] leading-relaxed text-ink-3">
                A directory path on the server; its contents are copied into the space
                knowledge base on sync.
              </p>
            </>
          )}
        </div>

        {/* Bottom: right-aligned Cancel / Add */}
        <div className="flex shrink-0 justify-end gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3 py-1.5 text-[12.5px] text-ink-2 transition-colors hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={submitting || !name.trim()}
            className="rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {submitting ? 'Creating…' : 'Add'}
          </button>
        </div>
      </div>
    </div>
  )
}
