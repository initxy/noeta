import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { spaceSkillsApi } from '../api/endpoints'
import type { PreviewContent, PreviewEntry, Skill } from '../api/types'
import { Markdown } from './Markdown'
import { cn } from '../lib/cn'
import { relativeTime } from '../lib/time'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import {
  IconBook,
  IconCheck,
  IconChevron,
  IconClose,
  IconFile,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSkill,
  IconTrash,
  IconUpload,
} from './icons'

/**
 * Skills configuration page: header + installed-skills management.
 * - Installed skills render as collapsible user-defined groups (ungrouped bucket +
 *   named groups + builtin group; search forces all groups open). Grouping is a pure
 *   display-layer organization (it does not affect assembly); owners multi-select
 *   rows and batch "move to group" / "delete". Rows are not split by source within a
 *   group (source is an implementation detail; rows installed with a version show it
 *   inline). Every row has an enabled toggle (owner only; disabled skills are not
 *   assembled into new sessions); owners can upload / delete / group. Installed
 *   skills can be previewed (file tree + content viewer); builtins get no preview
 *   entry, are pinned to the "Builtin" group, and cannot be grouped.
 * Deletion goes through a confirmation dialog.
 */

export function SkillsPage() {
  const { currentSpaceId, currentSpace } = useSpace()
  const { toast } = useToast()

  // Installed skills: single authoritative list, one fetch (builtin + space skills together).
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [togglingName, setTogglingName] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // Preview (installed space skills).
  const [previewName, setPreviewName] = useState<string | null>(null)
  // Delete confirmation (single unified endpoint).
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)

  // Multi-select (batch delete / batch group) keyed by skill name (unique within a
  // space). Only installed rows are selectable; builtins are not.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)

  const isOwner = currentSpace?.my_role === 'owner'

  const load = useCallback(async () => {
    if (!currentSpaceId) return
    setLoading(true)
    setError(null)
    try {
      // Single authoritative table: one fetch returns builtin + space skills; the
      // backend joins in per-skill metadata — no client-side list merging.
      const reg = await spaceSkillsApi.list(currentSpaceId)
      setSkills(reg.skills)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [currentSpaceId])

  useEffect(() => {
    void load()
  }, [load])

  const onPick = useCallback(
    async (file: File | undefined) => {
      if (!file || !currentSpaceId) return
      setUploading(true)
      try {
        const r = await spaceSkillsApi.upload(currentSpaceId, file)
        toast(`Installed skill "${r.skill.name}" — start a new session to use it`, 'info')
        await load()
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Upload failed')
      } finally {
        setUploading(false)
      }
    },
    [currentSpaceId, load, toast],
  )

  const doDelete = useCallback(async () => {
    if (!currentSpaceId || !deleteTarget) return
    const name = deleteTarget
    try {
      await spaceSkillsApi.remove(currentSpaceId, name)
      toast(`Deleted skill "${name}"`, 'info')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed')
    } finally {
      setDeleteTarget(null)
    }
  }, [currentSpaceId, deleteTarget, load, toast])

  const onToggle = useCallback(
    async (name: string, enabled: boolean) => {
      if (!currentSpaceId) return
      setTogglingName(name)
      try {
        await spaceSkillsApi.setEnabled(currentSpaceId, name, enabled)
        // Update locally without a full reload (toggling is frequent; avoid list flicker).
        setSkills((prev) =>
          prev.map((s) => (s.name === name ? { ...s, enabled } : s)),
        )
        toast(
          enabled
            ? `Enabled "${name}" — takes effect in new sessions`
            : `Disabled "${name}" — takes effect in new sessions`,
          'info',
        )
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Operation failed')
      } finally {
        setTogglingName(null)
      }
    },
    [currentSpaceId, toast],
  )

  // Names of installed rows (non-builtin); used to drop stale selections.
  const installedNames = useMemo(
    () =>
      new Set(skills.filter((s) => s.source !== 'builtin').map((s) => s.name)),
    [skills],
  )
  // After the list changes (delete / reinstall / space switch), drop selections that
  // no longer exist to avoid stale entries.
  useEffect(() => {
    setSelected((prev) => {
      let changed = false
      const next = new Set<string>()
      for (const n of prev) {
        if (installedNames.has(n)) next.add(n)
        else changed = true
      }
      return changed ? next : prev
    })
  }, [installedNames])

  const toggleSelect = useCallback((name: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])
  const clearSelection = useCallback(() => setSelected(new Set()), [])

  // Batch grouping: call the single-item setGroup endpoint concurrently per selected
  // skill (group=null moves out of the group).
  const batchAssign = useCallback(
    async (group: string | null) => {
      if (!currentSpaceId) return
      const names = [...selected]
      if (names.length === 0) return
      setBatchBusy(true)
      try {
        const results = await Promise.allSettled(
          names.map((n) => spaceSkillsApi.setGroup(currentSpaceId, n, group)),
        )
        const failed = results.filter((r) => r.status === 'rejected').length
        if (failed === 0) {
          toast(
            group
              ? `Moved ${names.length} skill${names.length === 1 ? '' : 's'} to group "${group}"`
              : `Removed ${names.length} skill${names.length === 1 ? '' : 's'} from their group`,
            'info',
          )
        } else {
          toast(`${names.length - failed} succeeded, ${failed} failed`, 'error')
        }
        setSelected(new Set())
        await load()
      } finally {
        setBatchBusy(false)
      }
    },
    [currentSpaceId, selected, load, toast],
  )

  // Batch delete: single unified endpoint (the backend owns the single authoritative table).
  const batchDelete = useCallback(async () => {
    if (!currentSpaceId) return
    const names = [...selected]
    if (names.length === 0) return
    setBatchBusy(true)
    try {
      const results = await Promise.allSettled(
        names.map((n) => spaceSkillsApi.remove(currentSpaceId, n)),
      )
      const failed = results.filter((r) => r.status === 'rejected').length
      if (failed === 0) {
        toast(`Deleted ${names.length} skill${names.length === 1 ? '' : 's'}`, 'info')
      } else {
        toast(`Deleted ${names.length - failed}, ${failed} failed`, 'error')
      }
      setSelected(new Set())
      await load()
    } finally {
      setBatchBusy(false)
      setBatchDeleteOpen(false)
    }
  }, [currentSpaceId, selected, load, toast])

  const builtin = skills.filter((s) => s.source === 'builtin')
  const spaceSkills = skills.filter((s) => s.source !== 'builtin')
  const total = skills.length

  // Local client-side filtering: case-insensitive substring match on name + description.
  const q = query.trim().toLowerCase()
  const match = (name: string, desc: string) =>
    !q || name.toLowerCase().includes(q) || (desc || '').toLowerCase().includes(q)
  const fBuiltin = builtin.filter((s) => match(s.name, s.description))
  const fSpace = spaceSkills.filter((s) => match(s.name, s.description))
  const visibleTotal = fBuiltin.length + fSpace.length
  // Installed = everything non-builtin (source is an implementation detail; rows are
  // not grouped by source), sorted by name.
  const fInstalled: Skill[] = [...fSpace].sort((a, b) =>
    a.name.localeCompare(b.name),
  )

  // User groups: named groups (sorted by name) + the ungrouped bucket. Without any
  // named group the bucket keeps the "Installed" title; once a named group exists it
  // becomes "Ungrouped" to tell them apart.
  const groupNames = Array.from(
    new Set(fInstalled.map((s) => s.group).filter((g): g is string => !!g)),
  ).sort((a, b) => a.localeCompare(b))
  const ungroupedRows = fInstalled.filter((s) => !s.group)
  const ungroupedTitle = groupNames.length > 0 ? 'Ungrouped' : 'Installed'
  // The group menu offers the full set of existing group names (unaffected by search
  // filtering) so group names don't vanish while searching.
  const allGroupNames = Array.from(
    new Set(
      spaceSkills.map((s) => s.group).filter((g): g is string => !!g),
    ),
  ).sort((a, b) => a.localeCompare(b))

  // Single-row renderer reused across groups to avoid duplicated JSX. Rows installed
  // with a version show it inline.
  const renderInstalledRow = (skill: Skill) => (
    <SkillRow
      key={skill.name}
      name={skill.name}
      description={skill.description}
      enabled={skill.enabled !== false}
      selectable={!!isOwner}
      selected={selected.has(skill.name)}
      onSelectChange={() => toggleSelect(skill.name)}
      meta={skill.version ? `v${skill.version}` : undefined}
      metaTitle={
        skill.version
          ? `v${skill.version}${
              skill.installed_at
                ? ` · installed ${relativeTime(skill.installed_at)}`
                : ''
            }`
          : undefined
      }
      canToggle={!!isOwner}
      toggling={togglingName === skill.name}
      onToggle={(next) => void onToggle(skill.name, next)}
      actions={
        <>
          <RowAction title="Preview skill" onClick={() => setPreviewName(skill.name)}>
            <IconFile className="h-3.5 w-3.5" />
          </RowAction>
          {isOwner && (
            <RowAction
              danger
              title="Delete skill"
              onClick={() => setDeleteTarget(skill.name)}
            >
              <IconTrash className="h-3.5 w-3.5" />
            </RowAction>
          )}
        </>
      }
    />
  )

  return (
    <div className="flex h-full w-full flex-col">
      <input
        ref={fileRef}
        type="file"
        accept=".md,.zip"
        className="hidden"
        onChange={(e) => {
          void onPick(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-8">
          {/* Header (left-aligned, sharing the toolbar/list left edge so a centered
              header doesn't misalign with the full-width toolbar). */}
          <h1 className="text-[20px] font-semibold text-ink">Skills</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
            Manage this space's skills: upload custom skills; toggles and changes take
            effect in newly created sessions.
          </p>

          {/* Toolbar: count on the left / search + upload on the right. */}
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="font-mono text-[11.5px] text-ink-3">
              {loading
                ? 'Loading…'
                : q
                  ? `${visibleTotal} / ${total} skill${total === 1 ? '' : 's'}`
                  : `${total} skill${total === 1 ? '' : 's'}`}
            </span>
            <div className="flex items-center gap-1.5">
              <div className="flex h-7 items-center gap-1.5 rounded-lg border border-border bg-bg px-2.5">
                <IconSearch className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search skills…"
                  className="w-44 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
                />
              </div>
              <button
                type="button"
                onClick={() => void load()}
                title="Refresh skill list"
                className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
              >
                <IconRefresh className="h-3.5 w-3.5" />
              </button>
              {isOwner && (
                <button
                  type="button"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                  title="Upload a skill file (.md / .zip)"
                  className="flex h-7 items-center gap-1 rounded-lg bg-accent px-2.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  <IconUpload className="h-3.5 w-3.5" />
                  {uploading ? 'Uploading…' : 'Add skill'}
                </button>
              )}
            </div>
          </div>

          {/* List */}
          <div className="mt-4">
            {loading ? (
              <div className="space-y-2">
                {[0, 1].map((i) => (
                  <div
                    key={i}
                    className="h-14 animate-pulse rounded-lg bg-surface-2"
                  />
                ))}
              </div>
            ) : error ? (
              <p className="py-6 text-center text-[12.5px] leading-relaxed text-danger">
                {error}
              </p>
            ) : total === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No skills yet
                {isOwner
                  ? ' — use "Add skill" in the top right to install one into this space.'
                  : '.'}
              </p>
            ) : visibleTotal === 0 ? (
              <p className="py-10 text-center text-[12.5px] leading-relaxed text-ink-3">
                No skills match "{query}".
              </p>
            ) : (
              <>
                {isOwner && selected.size > 0 && (
                  <BatchBar
                    count={selected.size}
                    busy={batchBusy}
                    groups={allGroupNames}
                    allSelected={
                      fInstalled.length > 0 &&
                      fInstalled.every((s) => selected.has(s.name))
                    }
                    onToggleAll={() =>
                      setSelected((prev) =>
                        fInstalled.length > 0 &&
                        fInstalled.every((s) => prev.has(s.name))
                          ? new Set()
                          : new Set(fInstalled.map((s) => s.name)),
                      )
                    }
                    onAssign={(g) => void batchAssign(g)}
                    onDelete={() => setBatchDeleteOpen(true)}
                    onClear={clearSelection}
                  />
                )}
                {groupNames.map((g) => (
                  <SkillGroup
                    key={g}
                    title={g}
                    count={fInstalled.filter((s) => s.group === g).length}
                    forceOpen={!!q}
                  >
                    {fInstalled
                      .filter((s) => s.group === g)
                      .map(renderInstalledRow)}
                  </SkillGroup>
                ))}
                {ungroupedRows.length > 0 && (
                  <SkillGroup
                    title={ungroupedTitle}
                    count={ungroupedRows.length}
                    forceOpen={!!q}
                  >
                    {ungroupedRows.map(renderInstalledRow)}
                  </SkillGroup>
                )}
                <SkillGroup title="Builtin" count={fBuiltin.length} forceOpen={!!q}>
                  {fBuiltin.map((s) => (
                    <SkillRow
                      key={s.name}
                      name={s.name}
                      description={s.description}
                      enabled
                      canToggle={false}
                      toggling={false}
                      onToggle={() => {}}
                      hideToggle
                    />
                  ))}
                </SkillGroup>
                <p className="mt-4 text-[11px] leading-snug text-ink-3">
                  Installed skills are visible to this space only; toggles and changes
                  take effect in newly created sessions — sessions already in progress
                  must be recreated to pick them up. Use the checkboxes to batch-delete
                  or move skills into groups (groups only organize the display and do
                  not affect assembly). Builtin skills are provided by the platform and
                  assembled into sessions automatically; they cannot be disabled,
                  previewed, deleted, or grouped here (they are managed centrally by
                  the platform admin).
                </p>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Preview viewer (installed space skills; builtins have no entry point). */}
      {previewName && currentSpaceId && (
        <PreviewModal
          spaceId={currentSpaceId}
          name={previewName}
          onClose={() => setPreviewName(null)}
        />
      )}

      {/* Delete confirmation (single) */}
      {deleteTarget && (
        <ConfirmModal
          title={`Delete skill "${deleteTarget}"?`}
          body="New sessions will no longer load this skill; sessions already in progress must be recreated to pick this up. This cannot be undone."
          confirmLabel="Delete"
          danger
          onConfirm={() => void doDelete()}
          onClose={() => setDeleteTarget(null)}
        />
      )}

      {/* Delete confirmation (batch) */}
      {batchDeleteOpen && (
        <ConfirmModal
          title={`Delete the ${selected.size} selected skill${selected.size === 1 ? '' : 's'}?`}
          body="New sessions will no longer load these skills; sessions already in progress must be recreated to pick this up. This cannot be undone."
          confirmLabel="Delete"
          danger
          onConfirm={() => void batchDelete()}
          onClose={() => setBatchDeleteOpen(false)}
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------ Installed-skill groups

/** Collapsible group: the header (chevron + title + count) toggles; search forces open. */
function SkillGroup({
  title,
  count,
  forceOpen,
  children,
}: {
  title: string
  count: number
  forceOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(true)
  if (count === 0) return null
  const shown = forceOpen || open
  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 rounded px-1 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ink-3 transition-colors hover:text-ink"
      >
        <IconChevron className="h-3 w-3" open={shown} />
        {title} ({count})
      </button>
      {shown && <ul className="mt-0.5 space-y-0.5">{children}</ul>}
    </div>
  )
}

/** Controlled checkbox: shows a check mark when checked. */
function Checkbox({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: () => void
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={onChange}
      className={cn(
        'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors',
        checked
          ? 'border-accent bg-accent text-accent-ink'
          : 'border-border-strong bg-bg hover:border-accent',
      )}
    >
      {checked && <IconCheck className="h-3 w-3" />}
    </button>
  )
}

/** Batch action bar (shown while rows are selected): count + select all + move to group + delete + cancel. */
function BatchBar({
  count,
  busy,
  groups,
  allSelected,
  onToggleAll,
  onAssign,
  onDelete,
  onClear,
}: {
  count: number
  busy: boolean
  groups: string[]
  allSelected: boolean
  onToggleAll: () => void
  onAssign: (group: string | null) => void
  onDelete: () => void
  onClear: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  const closeMenu = () => {
    setMenuOpen(false)
    setCreating(false)
    setNewName('')
  }
  const submitNew = () => {
    const g = newName.trim()
    if (!g) return
    closeMenu()
    onAssign(g)
  }

  return (
    <div className="mb-3 flex items-center gap-2 rounded-lg border border-accent/40 bg-accent-soft px-3 py-2">
      <span className="text-[12px] font-medium text-ink">{count} selected</span>
      <button
        type="button"
        onClick={onToggleAll}
        className="text-[11.5px] text-ink-3 transition-colors hover:text-ink"
      >
        {allSelected ? 'Deselect all' : 'Select all'}
      </button>
      <div className="ml-auto flex items-center gap-1.5">
        <div className="relative">
          <button
            type="button"
            onClick={() => (menuOpen ? closeMenu() : setMenuOpen(true))}
            disabled={busy}
            className="flex h-7 items-center gap-1 rounded-lg border border-border bg-bg px-2.5 text-[12px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-50"
          >
            <IconBook className="h-3.5 w-3.5" />
            Move to group
            <IconChevron className="h-3.5 w-3.5" open={menuOpen} />
          </button>
          {menuOpen && (
            <>
              <button
                type="button"
                aria-label="Close"
                onClick={closeMenu}
                className="fixed inset-0 z-40"
              />
              <div className="absolute right-0 z-50 mt-1 max-h-72 w-56 overflow-y-auto rounded-lg border border-border bg-surface py-1 shadow-[var(--shadow)]">
                {groups.map((g) => (
                  <button
                    key={g}
                    type="button"
                    onClick={() => {
                      closeMenu()
                      onAssign(g)
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] text-ink-2 hover:bg-surface-2"
                  >
                    <IconBook className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                    <span className="truncate">{g}</span>
                  </button>
                ))}
                {creating ? (
                  <div className="px-2 py-1.5">
                    <input
                      autoFocus
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') submitNew()
                        else if (e.key === 'Escape') closeMenu()
                      }}
                      maxLength={32}
                      placeholder="New group name; Enter to confirm"
                      className="w-full rounded-md border border-border bg-bg px-2 py-1 text-[12px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
                    />
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setCreating(true)}
                    className="flex w-full items-center gap-2 border-t border-border px-3 py-1.5 text-left text-[12px] text-ink-2 hover:bg-surface-2"
                  >
                    <IconPlus className="h-3.5 w-3.5 shrink-0" />
                    New group…
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    closeMenu()
                    onAssign(null)
                  }}
                  className="flex w-full items-center gap-2 border-t border-border px-3 py-1.5 text-left text-[12px] text-ink-3 hover:bg-surface-2"
                >
                  <IconClose className="h-3.5 w-3.5 shrink-0" />
                  Remove from group
                </button>
              </div>
            </>
          )}
        </div>
        <button
          type="button"
          onClick={onDelete}
          disabled={busy}
          className="flex h-7 items-center gap-1 rounded-lg px-2.5 text-[12px] font-medium text-danger transition-colors hover:bg-danger/10 disabled:opacity-50"
        >
          <IconTrash className="h-3.5 w-3.5" />
          Delete
        </button>
        <button
          type="button"
          onClick={onClear}
          disabled={busy}
          className="flex h-7 items-center rounded-lg px-2.5 text-[12px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/** Compact single skill row: optional checkbox + name + description (truncated;
 * title shows the full text) + meta + hover actions + enabled toggle. The checkbox
 * only renders when `selectable` (builtin rows are not selectable; a spacer keeps
 * alignment). */
function SkillRow({
  name,
  description,
  enabled,
  selectable,
  selected,
  onSelectChange,
  meta,
  metaTitle,
  canToggle,
  toggling,
  onToggle,
  hideToggle,
  actions,
}: {
  name: string
  description: string
  enabled: boolean
  selectable?: boolean
  selected?: boolean
  onSelectChange?: () => void
  meta?: string
  metaTitle?: string
  canToggle: boolean
  toggling: boolean
  onToggle: (next: boolean) => void
  hideToggle?: boolean
  actions?: ReactNode
}) {
  return (
    <li
      className={cn(
        'group flex items-center gap-2.5 rounded-lg px-3 py-1.5 hover:bg-surface-2',
        selected && 'bg-accent-soft',
      )}
    >
      {selectable ? (
        <Checkbox checked={!!selected} onChange={() => onSelectChange?.()} />
      ) : (
        <span className="h-4 w-4 shrink-0" />
      )}
      <IconSkill
        className={cn('h-4 w-4 shrink-0', enabled ? 'text-accent' : 'text-ink-3')}
      />
      <div
        className={cn(
          'flex min-w-0 flex-1 items-baseline gap-2',
          !enabled && 'opacity-50',
        )}
      >
        <p className="shrink-0 font-mono text-[12.5px] text-ink">{name}</p>
        <p
          title={description || undefined}
          className="min-w-0 flex-1 truncate text-[11.5px] text-ink-3"
        >
          {description || '(no description)'}
        </p>
      </div>
      {meta && (
        <span
          title={metaTitle}
          className="hidden shrink-0 font-mono text-[10.5px] text-ink-3 sm:block"
        >
          {meta}
        </span>
      )}
      {actions && (
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition group-hover:opacity-100">
          {actions}
        </div>
      )}
      {hideToggle ? (
        // Builtin skills: assembled globally, cannot be turned off per space; a
        // read-only placeholder instead of a toggle.
        <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] text-ink-3">
          Auto-assembled
        </span>
      ) : (
        <Switch
          on={enabled}
          disabled={!canToggle}
          busy={toggling}
          onChange={onToggle}
        />
      )}
    </li>
  )
}

/** Inline hover action button (preview / delete). */
function RowAction({
  title,
  danger,
  disabled,
  onClick,
  children,
}: {
  title: string
  danger?: boolean
  disabled?: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'flex h-6 w-6 items-center justify-center rounded text-ink-3 disabled:opacity-50',
        danger ? 'hover:text-danger' : 'hover:text-accent',
      )}
    >
      {children}
    </button>
  )
}

/** Enabled toggle: owners can switch (disabled skills are not assembled into new
 * sessions); non-owners see a read-only state. */
function Switch({
  on,
  disabled,
  busy,
  onChange,
}: {
  on: boolean
  disabled?: boolean
  busy?: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled || busy}
      onClick={() => onChange(!on)}
      title={
        disabled
          ? on
            ? 'Enabled (owner only)'
            : 'Disabled (owner only)'
          : on
            ? 'Disable skill (takes effect in new sessions)'
            : 'Enable skill (takes effect in new sessions)'
      }
      className={cn(
        'relative h-4 w-7 shrink-0 rounded-full transition-colors',
        on ? 'bg-accent' : 'bg-border-strong',
        disabled ? 'cursor-default' : 'cursor-pointer',
        busy && 'opacity-50',
      )}
    >
      <span
        className={cn(
          'absolute left-0.5 top-0.5 h-3 w-3 rounded-full bg-white transition-transform',
          on && 'translate-x-3',
        )}
      />
    </button>
  )
}

// ------------------------------------------------------------ Preview viewer

/** Installed-skill preview: file tree on the left + file content on the right (via the preview endpoint). */
function PreviewModal({
  spaceId,
  name,
  onClose,
}: {
  spaceId: string
  name: string
  onClose: () => void
}) {
  const { toast } = useToast()
  const [entries, setEntries] = useState<PreviewEntry[]>([])
  const [activePath, setActivePath] = useState<string | null>(null)
  const [content, setContent] = useState<PreviewContent | null>(null)
  const [loadingContent, setLoadingContent] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selectFile = useCallback(
    async (path: string) => {
      setActivePath(path)
      setLoadingContent(true)
      setContent(null)
      try {
        const r = await spaceSkillsApi.preview(spaceId, name, path)
        setContent(r as PreviewContent)
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to load file')
      } finally {
        setLoadingContent(false)
      }
    },
    [spaceId, name, toast],
  )

  // Fetch the file tree.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await spaceSkillsApi.preview(spaceId, name)
        if (cancelled) return
        const list = (r as { entries: PreviewEntry[] }).entries
        setEntries(list)
        // Default to the first file.
        const firstFile = list.find((e) => !e.is_dir)
        if (firstFile) void selectFile(firstFile.path)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [spaceId, name, selectFile])

  // Sort: directories first, then alphabetical by path.
  const sorted = useMemo(
    () =>
      [...entries].sort((a, b) => {
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
        return a.path.localeCompare(b.path)
      }),
    [entries],
  )

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={`Preview skill ${name}`}
        onClick={(e) => e.stopPropagation()}
        className="msg-enter flex h-[80vh] w-full max-w-4xl flex-col rounded-xl border border-border bg-surface shadow-[var(--shadow)]"
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <IconFile className="h-4 w-4 text-accent" />
            <h2 className="font-mono text-[14px] text-ink">{name}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-3.5 w-3.5" />
          </button>
        </div>

        {error ? (
          <p className="px-5 py-8 text-center text-[12.5px] text-danger">{error}</p>
        ) : (
          <div className="flex min-h-0 flex-1">
            {/* File tree */}
            <div className="w-56 shrink-0 overflow-y-auto border-r border-border bg-bg px-2 py-2">
              {sorted.length === 0 ? (
                <p className="px-2 py-4 text-center text-[11.5px] text-ink-3">
                  No files
                </p>
              ) : (
                <ul className="space-y-0.5">
                  {sorted.map((e) => {
                    const depth = e.path.split('/').length - 1
                    const active = activePath === e.path
                    return (
                      <li key={e.path}>
                        <button
                          type="button"
                          disabled={e.is_dir}
                          onClick={() => void selectFile(e.path)}
                          style={{ paddingLeft: 6 + depth * 12 }}
                          className={cn(
                            'flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11.5px]',
                            e.is_dir
                              ? 'cursor-default font-medium text-ink-2'
                              : active
                                ? 'bg-surface-2 text-ink'
                                : 'text-ink-3 hover:bg-surface-2 hover:text-ink',
                          )}
                        >
                          <IconFile
                            className={cn(
                              'h-3 w-3 shrink-0',
                              e.is_dir ? 'text-accent' : 'text-ink-3',
                            )}
                          />
                          <span className="truncate font-mono">
                            {e.path.split('/').pop()}
                          </span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            {/* File content */}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {loadingContent ? (
                <div className="space-y-2 p-4">
                  {[0, 1, 2].map((i) => (
                    <div
                      key={i}
                      className="h-4 animate-pulse rounded bg-surface-2"
                    />
                  ))}
                </div>
              ) : content ? (
                <div className="p-4">
                  <p className="mb-3 font-mono text-[10.5px] text-ink-3">
                    {content.path}
                    {content.truncated && ' · truncated'}
                    {content.binary && ' · binary'}
                  </p>
                  {content.binary ? (
                    <p className="text-[12.5px] text-ink-3">{content.content}</p>
                  ) : content.path.endsWith('.md') ? (
                    <Markdown text={content.content} />
                  ) : (
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-ink-2">
                      {content.content}
                    </pre>
                  )}
                </div>
              ) : (
                <p className="p-8 text-center text-[12.5px] text-ink-3">
                  Select a file on the left to view its content
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

// ------------------------------------------------------------ Shared confirmation dialog

function ConfirmModal({
  title,
  body,
  confirmLabel,
  danger,
  onConfirm,
  onClose,
}: {
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        className="msg-enter w-full max-w-sm rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]"
      >
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        <p className="mt-2 text-[12.5px] leading-relaxed text-ink-3">{body}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-[12.5px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={cn(
              'rounded-lg px-3 py-1.5 text-[12.5px] font-medium text-white transition-opacity hover:opacity-90',
              danger ? 'bg-danger' : 'bg-accent text-accent-ink',
            )}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
