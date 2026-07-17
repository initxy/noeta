import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { adminApi, skillsApi } from '../../api/endpoints'
import type {
  AdminConfigItem,
  AdminSession,
  AdminSpace,
  AdminStats,
  AdminUser,
  KnowledgeSource,
  PreviewContent,
  PreviewEntry,
  Skill,
  SpaceMember,
} from '../../api/types'
import { cn } from '../../lib/cn'
import { relativeTime } from '../../lib/time'
import { useToast } from '../../state/toast'
import { Markdown } from '../Markdown'
import { IconClose, IconFile, IconRefresh, IconSearch, IconTrash, IconUpload } from '../icons'
import { TracePage } from '../TracePage'

// Admin sub-pages: full-page left sub-nav + right content area (no routing
// framework; the same state-based switching as the main view).
type SubView =
  | 'overview'
  | 'users'
  | 'tasks'
  | 'spaces'
  | 'skills'
  | 'config'
  | 'trace'

const SUBS: { key: SubView; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'users', label: 'Users' },
  { key: 'tasks', label: 'Tasks' },
  { key: 'spaces', label: 'Spaces' },
  { key: 'skills', label: 'Builtin skills' },
  { key: 'config', label: 'Dynamic config' },
  { key: 'trace', label: 'Trace' },
]

const PAGE_SIZE = 50

// Status badge colors: shared by sessions (idle/running/waiting) and knowledge
// sources (pending/syncing/ready/failed).
const STATUS_STYLE: Record<string, string> = {
  idle: 'bg-surface-2 text-ink-2',
  running: 'bg-accent-soft text-accent',
  waiting: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  pending: 'bg-surface-2 text-ink-2',
  syncing: 'bg-accent-soft text-accent',
  ready: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  failed: 'bg-danger-soft text-danger',
}

function StatusBadge({ status }: { status?: string }) {
  if (!status) return <span className="text-ink-3">—</span>
  return (
    <span
      className={cn(
        'inline-flex rounded px-1.5 py-0.5 font-mono text-[10.5px]',
        STATUS_STYLE[status] ?? 'bg-surface-2 text-ink-2',
      )}
    >
      {status}
    </span>
  )
}

/** Table pager footer: shown range + previous / next. */
function Pager({
  total,
  offset,
  count,
  onPrev,
  onNext,
}: {
  total: number
  offset: number
  count: number
  onPrev: () => void
  onNext: () => void
}) {
  const from = total === 0 ? 0 : offset + 1
  const to = offset + count
  return (
    <div className="flex shrink-0 items-center justify-between border-t border-border px-4 py-2.5">
      <span className="font-mono text-[11px] text-ink-3">
        {from}–{to} / {total}
      </span>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={onPrev}
          disabled={offset === 0}
          className="rounded-lg border border-border bg-bg px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={to >= total}
          className="rounded-lg border border-border bg-bg px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function PanelShell({
  title,
  desc,
  toolbar,
  children,
  footer,
}: {
  title: string
  desc?: string
  toolbar?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="shrink-0 border-b border-border px-5 py-3.5">
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        {desc && <p className="mt-0.5 text-[12px] text-ink-3">{desc}</p>}
        {toolbar && <div className="mt-3">{toolbar}</div>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      {footer}
    </div>
  )
}

function SearchBox({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  placeholder: string
}) {
  return (
    <div className="flex h-7 w-56 items-center gap-1.5 rounded-lg border border-border bg-bg px-2.5">
      <IconSearch className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
      />
    </div>
  )
}

// ------------------------------------------------------------------ Overview
function Overview() {
  const { toast } = useToast()
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    adminApi
      .stats()
      .then((s) => alive && setStats(s))
      .catch((e) => alive && toast(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [toast])

  const cards: { label: string; value: string | number; sub?: string }[] = stats
    ? [
        { label: 'Users', value: stats.users },
        {
          label: 'Tasks (sessions)',
          value: stats.sessions.total,
          sub: Object.entries(stats.sessions.by_status)
            .map(([k, v]) => `${k} ${v}`)
            .join(' · '),
        },
        { label: 'Spaces', value: stats.spaces },
        {
          label: 'Knowledge sources',
          value: stats.knowledge_sources.total,
          sub: Object.entries(stats.knowledge_sources.by_status)
            .map(([k, v]) => `${k} ${v}`)
            .join(' · '),
        },
        { label: 'Builtin skills', value: stats.builtin_skills },
        { label: 'Space skills', value: stats.space_skills },
      ]
    : []

  return (
    <PanelShell title="Overview" desc="A snapshot of platform entity counts.">
      <div className="p-5">
        {loading ? (
          <p className="text-[13px] text-ink-3">Loading…</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {cards.map((c) => (
              <div
                key={c.label}
                className="rounded-xl border border-border bg-surface p-4"
              >
                <p className="text-[12px] text-ink-3">{c.label}</p>
                <p className="mt-1 text-[24px] font-semibold text-ink">{c.value}</p>
                {c.sub && (
                  <p className="mt-1 font-mono text-[10.5px] text-ink-3">{c.sub}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </PanelShell>
  )
}

// ------------------------------------------------------------------ Users
function UsersPanel() {
  const { toast } = useToast()
  const [rows, setRows] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(
    (off: number, query: string) => {
      setLoading(true)
      adminApi
        .users(query, off, PAGE_SIZE)
        .then((r) => {
          setRows(r.users)
          setTotal(r.total)
        })
        .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load'))
        .finally(() => setLoading(false))
    },
    [toast],
  )

  // Debounced search: a q change goes back to page one and requeries.
  useEffect(() => {
    const t = setTimeout(() => {
      setOffset(0)
      load(0, q.trim())
    }, 250)
    return () => clearTimeout(t)
  }, [q, load])

  const goto = (off: number) => {
    setOffset(off)
    load(off, q.trim())
  }

  return (
    <PanelShell
      title="Users"
      desc="Everyone who has ever signed in to the platform."
      toolbar={<SearchBox value={q} onChange={setQ} placeholder="Search username / email / name…" />}
      footer={
        <Pager
          total={total}
          offset={offset}
          count={rows.length}
          onPrev={() => goto(Math.max(0, offset - PAGE_SIZE))}
          onNext={() => goto(offset + PAGE_SIZE)}
        />
      }
    >
      <table className="w-full text-[12.5px]">
        <thead className="sticky top-0 bg-surface text-left text-[11px] text-ink-3">
          <tr className="border-b border-border">
            <th className="px-5 py-2 font-medium">User</th>
            <th className="px-3 py-2 font-medium">Email</th>
            <th className="px-3 py-2 font-medium">Registered</th>
            <th className="px-5 py-2 font-medium">Last active</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <tr key={u.username} className="border-b border-border/60 hover:bg-surface-2">
              <td className="px-5 py-2">
                <div className="flex items-center gap-2">
                  {u.avatar ? (
                    <img src={u.avatar} alt="" className="h-6 w-6 rounded-full object-cover" />
                  ) : (
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent-soft text-[11px] font-medium uppercase text-ink">
                      {(u.name || u.username).charAt(0)}
                    </span>
                  )}
                  <div className="min-w-0">
                    <div className="truncate text-ink">{u.name || u.username}</div>
                    {u.name && (
                      <div className="truncate font-mono text-[10.5px] text-ink-3">
                        {u.username}
                      </div>
                    )}
                  </div>
                </div>
              </td>
              <td className="px-3 py-2 text-ink-2">{u.email || '—'}</td>
              <td className="px-3 py-2 text-ink-3">{relativeTime(u.created_at)}</td>
              <td className="px-5 py-2 text-ink-3">{relativeTime(u.updated_at)}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr>
              <td colSpan={4} className="px-5 py-8 text-center text-ink-3">
                No users.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </PanelShell>
  )
}

// ------------------------------------------------------------------ Tasks (sessions)
const SESSION_STATUSES = ['idle', 'running', 'waiting']

function TasksPanel({ onViewTrace }: { onViewTrace: (id: string) => void }) {
  const { toast } = useToast()
  const [rows, setRows] = useState<AdminSession[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [userFilter, setUserFilter] = useState('')
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(
    (off: number, user: string, st: string) => {
      setLoading(true)
      adminApi
        .sessions(
          { user: user || undefined, status: st || undefined },
          off,
          PAGE_SIZE,
        )
        .then((r) => {
          setRows(r.sessions)
          setTotal(r.total)
        })
        .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load'))
        .finally(() => setLoading(false))
    },
    [toast],
  )

  useEffect(() => {
    const t = setTimeout(() => {
      setOffset(0)
      load(0, userFilter.trim(), status)
    }, 250)
    return () => clearTimeout(t)
  }, [userFilter, status, load])

  const goto = (off: number) => {
    setOffset(off)
    load(off, userFilter.trim(), status)
  }

  return (
    <PanelShell
      title="Tasks"
      desc="Execution records of every session (task) on the platform."
      toolbar={
        <div className="flex items-center gap-2">
          <SearchBox value={userFilter} onChange={setUserFilter} placeholder="Filter by user…" />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="h-7 cursor-pointer rounded-lg border border-border bg-bg px-2 font-mono text-[11.5px] text-ink-2 outline-none focus:border-accent"
          >
            <option value="">status: all</option>
            {SESSION_STATUSES.map((s) => (
              <option key={s} value={s}>
                status: {s}
              </option>
            ))}
          </select>
        </div>
      }
      footer={
        <Pager
          total={total}
          offset={offset}
          count={rows.length}
          onPrev={() => goto(Math.max(0, offset - PAGE_SIZE))}
          onNext={() => goto(offset + PAGE_SIZE)}
        />
      }
    >
      <table className="w-full text-[12.5px]">
        <thead className="sticky top-0 bg-surface text-left text-[11px] text-ink-3">
          <tr className="border-b border-border">
            <th className="px-5 py-2 font-medium">Title</th>
            <th className="px-3 py-2 font-medium">user</th>
            <th className="px-3 py-2 font-medium">Space</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Updated</th>
            <th className="px-5 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id} className="border-b border-border/60 hover:bg-surface-2">
              <td className="max-w-[220px] px-5 py-2">
                <div className="truncate text-ink" title={s.title}>
                  {s.title || 'New session'}
                </div>
              </td>
              <td className="px-3 py-2 font-mono text-[11.5px] text-ink-2">{s.user}</td>
              <td className="max-w-[140px] px-3 py-2">
                <span className="block truncate text-ink-2" title={s.space_name || ''}>
                  {s.space_name || '—'}
                </span>
              </td>
              <td className="px-3 py-2">
                <StatusBadge status={s.status} />
              </td>
              <td className="px-3 py-2 text-ink-3">{relativeTime(s.updated_at)}</td>
              <td className="px-5 py-2 text-right">
                <button
                  type="button"
                  onClick={() => onViewTrace(s.id)}
                  className="rounded-lg border border-border bg-bg px-2.5 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
                >
                  View trace
                </button>
              </td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr>
              <td colSpan={6} className="px-5 py-8 text-center text-ink-3">
                No tasks.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </PanelShell>
  )
}

// ------------------------------------------------------------------ Spaces + drilldown
type SpaceTab = 'members' | 'knowledge' | 'skills'

function SpaceDrilldown({ space, onBack }: { space: AdminSpace; onBack: () => void }) {
  const { toast } = useToast()
  const [tab, setTab] = useState<SpaceTab>('members')
  const [members, setMembers] = useState<SpaceMember[]>([])
  const [sources, setSources] = useState<KnowledgeSource[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    const p =
      tab === 'members'
        ? adminApi.spaceMembers(space.id).then((r) => alive && setMembers(r.members))
        : tab === 'knowledge'
          ? adminApi.spaceKnowledge(space.id).then((r) => alive && setSources(r.sources))
          : adminApi.spaceSkills(space.id).then((r) => alive && setSkills(r.skills))
    p.catch((e: unknown) => alive && toast(e instanceof Error ? e.message : 'Failed to load')).finally(
      () => alive && setLoading(false),
    )
    return () => {
      alive = false
    }
  }, [tab, space.id, toast])

  const tabs: { key: SpaceTab; label: string }[] = [
    { key: 'members', label: `Members (${space.member_count})` },
    { key: 'knowledge', label: 'Knowledge sources' },
    { key: 'skills', label: 'Skills' },
  ]

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="shrink-0 border-b border-border px-5 py-3.5">
        <button
          type="button"
          onClick={onBack}
          className="mb-2 text-[12px] text-ink-3 transition-colors hover:text-ink"
        >
          ← Back to spaces
        </button>
        <h2 className="text-[15px] font-semibold text-ink">
          {space.name}
          {space.is_personal && (
            <span className="ml-2 rounded bg-surface-2 px-1.5 py-0.5 text-[10.5px] font-normal text-ink-3">
              Personal space
            </span>
          )}
        </h2>
        <p className="mt-0.5 font-mono text-[11px] text-ink-3">
          owner {space.owner} · {space.session_count} session{space.session_count === 1 ? '' : 's'}
        </p>
        <div className="mt-3 flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={cn(
                'rounded-lg px-3 py-1.5 text-[12.5px] font-medium transition-colors',
                tab === t.key
                  ? 'bg-surface-2 text-ink'
                  : 'text-ink-3 hover:bg-surface-2 hover:text-ink',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {loading ? (
          <p className="text-[13px] text-ink-3">Loading…</p>
        ) : tab === 'members' ? (
          <ul className="space-y-1">
            {members.map((m) => (
              <li
                key={m.username}
                className="flex items-center gap-2 rounded-lg border border-border/60 px-3 py-2"
              >
                <span className="flex-1 truncate text-[12.5px] text-ink">
                  {m.name || m.username}
                  <span className="ml-2 font-mono text-[10.5px] text-ink-3">{m.username}</span>
                </span>
                <span className="font-mono text-[10.5px] text-ink-3">{m.role}</span>
              </li>
            ))}
            {members.length === 0 && <p className="text-[13px] text-ink-3">No members.</p>}
          </ul>
        ) : tab === 'knowledge' ? (
          <ul className="space-y-1">
            {sources.map((s) => (
              <li key={s.id} className="rounded-lg border border-border/60 px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="flex-1 truncate text-[12.5px] text-ink">{s.name}</span>
                  <span className="font-mono text-[10.5px] text-ink-3">{s.type}</span>
                  <StatusBadge status={s.status} />
                </div>
                {s.last_error && (
                  <p className="mt-1 truncate text-[11px] text-danger" title={s.last_error}>
                    {s.last_error}
                  </p>
                )}
              </li>
            ))}
            {sources.length === 0 && <p className="text-[13px] text-ink-3">No knowledge sources.</p>}
          </ul>
        ) : (
          <ul className="space-y-1">
            {skills.map((sk) => (
              <li
                key={sk.name}
                className="flex items-center gap-2 rounded-lg border border-border/60 px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[12.5px] text-ink">{sk.name}</div>
                  {sk.description && (
                    <div className="truncate text-[11px] text-ink-3">{sk.description}</div>
                  )}
                </div>
                {sk.group && (
                  <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10.5px] text-ink-3">
                    {sk.group}
                  </span>
                )}
                <span className="font-mono text-[10.5px] text-ink-3">{sk.source}</span>
                <span
                  className={cn(
                    'inline-flex rounded px-1.5 py-0.5 text-[10.5px]',
                    sk.enabled === false
                      ? 'bg-surface-2 text-ink-3'
                      : 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
                  )}
                >
                  {sk.enabled === false ? 'Disabled' : 'Enabled'}
                </span>
              </li>
            ))}
            {skills.length === 0 && <p className="text-[13px] text-ink-3">No skills.</p>}
          </ul>
        )}
      </div>
    </div>
  )
}

function SpacesPanel() {
  const { toast } = useToast()
  const [rows, setRows] = useState<AdminSpace[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<AdminSpace | null>(null)

  const load = useCallback(
    (off: number) => {
      setLoading(true)
      adminApi
        .spaces(off, PAGE_SIZE)
        .then((r) => {
          setRows(r.spaces)
          setTotal(r.total)
        })
        .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load'))
        .finally(() => setLoading(false))
    },
    [toast],
  )

  useEffect(() => {
    load(0)
  }, [load])

  const goto = (off: number) => {
    setOffset(off)
    load(off)
  }

  if (selected) {
    return <SpaceDrilldown space={selected} onBack={() => setSelected(null)} />
  }

  return (
    <PanelShell
      title="Spaces"
      desc="Every space on the platform; click a row to view members / knowledge sources / skills."
      footer={
        <Pager
          total={total}
          offset={offset}
          count={rows.length}
          onPrev={() => goto(Math.max(0, offset - PAGE_SIZE))}
          onNext={() => goto(offset + PAGE_SIZE)}
        />
      }
    >
      <table className="w-full text-[12.5px]">
        <thead className="sticky top-0 bg-surface text-left text-[11px] text-ink-3">
          <tr className="border-b border-border">
            <th className="px-5 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">owner</th>
            <th className="px-3 py-2 font-medium">Members</th>
            <th className="px-5 py-2 font-medium">Sessions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr
              key={s.id}
              onClick={() => setSelected(s)}
              className="cursor-pointer border-b border-border/60 hover:bg-surface-2"
            >
              <td className="px-5 py-2">
                <span className="text-ink">{s.name}</span>
                {s.is_personal && (
                  <span className="ml-2 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] text-ink-3">
                    Personal
                  </span>
                )}
              </td>
              <td className="px-3 py-2 font-mono text-[11.5px] text-ink-2">{s.owner}</td>
              <td className="px-3 py-2 text-ink-2">{s.member_count}</td>
              <td className="px-5 py-2 text-ink-2">{s.session_count}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr>
              <td colSpan={4} className="px-5 py-8 text-center text-ink-3">
                No spaces.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </PanelShell>
  )
}

// ------------------------------------------------------------------ Builtin skills
/** Enable/disable toggle (matching the dynamic-config page's switch styling). */
function Switch({
  checked,
  disabled,
  onClick,
}: {
  checked: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={onClick}
      title={checked ? 'Click to disable' : 'Click to enable'}
      className={cn(
        'relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50',
        checked ? 'bg-accent' : 'bg-surface-3',
      )}
    >
      <span
        className={cn(
          'absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform',
          checked ? 'translate-x-[18px]' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}

function BuiltinSkillsPanel() {
  const { toast } = useToast()
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [confirmName, setConfirmName] = useState<string | null>(null)
  const [busyName, setBusyName] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    skillsApi
      .list()
      .then((r) => setSkills(r.skills))
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const onPick = useCallback(
    async (file: File | undefined) => {
      if (!file) return
      setUploading(true)
      try {
        const r = await skillsApi.upload(file)
        toast(`Uploaded builtin skill "${r.skill.name}"`, 'info')
        load()
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Upload failed')
      } finally {
        setUploading(false)
      }
    },
    [load, toast],
  )

  const onToggle = useCallback(
    async (sk: Skill) => {
      setBusyName(sk.name)
      try {
        await skillsApi.setEnabled(sk.name, sk.enabled === false)
        load()
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Operation failed')
      } finally {
        setBusyName(null)
      }
    },
    [load, toast],
  )

  const onDelete = useCallback(
    async (name: string) => {
      try {
        await skillsApi.remove(name)
        setConfirmName(null)
        load()
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Delete failed')
      }
    },
    [load, toast],
  )

  return (
    <PanelShell
      title="Builtin skills"
      desc="Platform-provided skills, all in the shared directory and effective for every space: upload / disable / delete. Changes take effect in new sessions."
      toolbar={
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-1.5 rounded-lg border border-border bg-bg px-3 py-1.5 text-[12.5px] text-ink transition-colors hover:bg-surface-2 disabled:opacity-50"
          >
            <IconUpload className="h-3.5 w-3.5" />
            {uploading ? 'Uploading…' : 'Upload skill (.md / .zip)'}
          </button>
          <button
            type="button"
            onClick={load}
            title="Refresh"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <IconRefresh className="h-3.5 w-3.5" />
          </button>
        </div>
      }
    >
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
      <ul className="p-3">
        {skills.map((sk) => {
          const disabled = sk.enabled === false
          return (
            <li
              key={sk.name}
              className="group flex items-center gap-2 rounded-lg px-3 py-2 hover:bg-surface-2"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] text-ink">{sk.name}</div>
                {sk.description && (
                  <div className="truncate text-[11.5px] text-ink-3">
                    {sk.description}
                  </div>
                )}
              </div>
              <span
                className={cn(
                  'text-[10.5px]',
                  disabled ? 'text-ink-3' : 'text-emerald-600 dark:text-emerald-400',
                )}
              >
                {disabled ? 'Disabled' : 'Enabled'}
              </span>
              <Switch
                checked={!disabled}
                disabled={busyName === sk.name}
                onClick={() => void onToggle(sk)}
              />
              <button
                type="button"
                title="Preview content"
                onClick={() => setPreviewName(sk.name)}
                className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 opacity-0 transition hover:bg-surface-3 hover:text-ink group-hover:opacity-100"
              >
                <IconFile className="h-3.5 w-3.5" />
              </button>
              {confirmName === sk.name ? (
                <button
                  type="button"
                  onClick={() => void onDelete(sk.name)}
                  onBlur={() => setConfirmName(null)}
                  autoFocus
                  className="rounded-md bg-danger px-2 py-1 text-[11px] font-medium text-white"
                >
                  Confirm delete
                </button>
              ) : (
                <button
                  type="button"
                  title="Delete skill"
                  onClick={() => setConfirmName(sk.name)}
                  className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 hover:bg-surface-3 hover:text-danger"
                >
                  <IconTrash className="h-3.5 w-3.5" />
                </button>
              )}
            </li>
          )
        })}
        {!loading && skills.length === 0 && (
          <li className="px-3 py-8 text-center text-[13px] text-ink-3">
            No builtin skills yet — upload one via the button in the top right.
          </li>
        )}
      </ul>
      {previewName && (
        <BuiltinSkillPreview
          name={previewName}
          onClose={() => setPreviewName(null)}
        />
      )}
    </PanelShell>
  )
}

/** Builtin-skill content preview (admin read-only): file tree on the left + content on the right, via /skills/{name}/preview. */
function BuiltinSkillPreview({
  name,
  onClose,
}: {
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
        const r = await skillsApi.preview(name, path)
        setContent(r as PreviewContent)
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to load file')
      } finally {
        setLoadingContent(false)
      }
    },
    [name, toast],
  )

  useEffect(() => {
    let cancelled = false
    skillsApi
      .preview(name)
      .then((r) => {
        if (cancelled) return
        const list = (r as { entries: PreviewEntry[] }).entries
        setEntries(list)
        const firstFile = list.find((e) => !e.is_dir)
        if (firstFile) void selectFile(firstFile.path)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      })
    return () => {
      cancelled = true
    }
  }, [name, selectFile])

  const sorted = [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.path.localeCompare(b.path)
  })

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label={`Preview skill ${name}`}
        onClick={(e) => e.stopPropagation()}
        className="flex h-[80vh] w-full max-w-4xl flex-col rounded-xl border border-border bg-surface shadow-[var(--shadow)]"
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
            <div className="w-56 shrink-0 overflow-y-auto border-r border-border bg-bg px-2 py-2">
              {sorted.length === 0 ? (
                <p className="px-2 py-4 text-center text-[11.5px] text-ink-3">No files</p>
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
            <div className="min-h-0 flex-1 overflow-y-auto">
              {loadingContent ? (
                <div className="space-y-2 p-4">
                  {[0, 1, 2].map((i) => (
                    <div key={i} className="h-4 animate-pulse rounded bg-surface-2" />
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

// ------------------------------------------------------------------ Dynamic config
function ConfigPanel() {
  const { toast } = useToast()
  const [items, setItems] = useState<AdminConfigItem[]>([])
  const [loading, setLoading] = useState(true)
  const [savingKey, setSavingKey] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    adminApi
      .config()
      .then((r) => setItems(r.items))
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const onToggle = useCallback(
    async (item: AdminConfigItem) => {
      setSavingKey(item.key)
      try {
        const r = await adminApi.putConfig(item.key, !item.value)
        setItems((prev) => prev.map((it) => (it.key === item.key ? r.item : it)))
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Save failed')
      } finally {
        setSavingKey(null)
      }
    },
    [toast],
  )

  return (
    <PanelShell
      title="Dynamic config"
      desc="Changes take effect immediately (no restart); keys without an override fall back to the deployment's static default."
    >
      <div className="space-y-2 p-5">
        {loading ? (
          <p className="text-[13px] text-ink-3">Loading…</p>
        ) : (
          items.map((item) => (
            <div
              key={item.key}
              className="flex items-center gap-3 rounded-xl border border-border bg-surface p-4"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[12.5px] text-ink">{item.key}</span>
                  {item.overridden && (
                    <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
                      Overridden
                    </span>
                  )}
                </div>
                <p className="mt-1 text-[12px] text-ink-3">{item.description}</p>
                <p className="mt-1 font-mono text-[10.5px] text-ink-3">
                  default {String(item.default)}
                  {item.overridden && item.updated_by
                    ? ` · changed by ${item.updated_by} ${relativeTime(item.updated_at ?? 0)}`
                    : ''}
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={item.value}
                disabled={savingKey === item.key}
                onClick={() => void onToggle(item)}
                className={cn(
                  'relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50',
                  item.value ? 'bg-accent' : 'bg-surface-3',
                )}
              >
                <span
                  className={cn(
                    'absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform',
                    item.value ? 'translate-x-[22px]' : 'translate-x-0.5',
                  )}
                />
              </button>
            </div>
          ))
        )}
      </div>
    </PanelShell>
  )
}

// ------------------------------------------------------------------ Trace sub-page
function TraceSubPanel({ initialId }: { initialId: string | null }) {
  const [sessionId, setSessionId] = useState<string | null>(initialId)
  // The id brought in from the task list: sync when switching to the Trace sub-page.
  useEffect(() => {
    if (initialId) setSessionId(initialId)
  }, [initialId])
  return (
    <TracePage
      sessionId={sessionId}
      session={null}
      onSessionIdChange={setSessionId}
      // No chat SSE to rely on in the admin console; TracePage's own manual
      // refresh button does the incremental top-up.
      refreshKey={0}
    />
  )
}

// ------------------------------------------------------------------ Main component
export function AdminPage({
  initialTraceSessionId = null,
}: {
  initialTraceSessionId?: string | null
}) {
  const [sub, setSub] = useState<SubView>(
    initialTraceSessionId ? 'trace' : 'overview',
  )
  const [traceId, setTraceId] = useState<string | null>(initialTraceSessionId)

  const openTrace = useCallback((id: string) => {
    setTraceId(id)
    setSub('trace')
  }, [])

  useEffect(() => {
    if (!initialTraceSessionId) return
    openTrace(initialTraceSessionId)
  }, [initialTraceSessionId, openTrace])

  return (
    <div className="flex min-h-0 flex-1">
      {/* Left sub-navigation */}
      <nav className="flex w-40 shrink-0 flex-col gap-0.5 border-r border-border p-2">
        {SUBS.map((s) => (
          <button
            key={s.key}
            type="button"
            onClick={() => setSub(s.key)}
            className={cn(
              'flex items-center gap-2 rounded-lg px-3 py-2 text-left text-[12.5px] transition-colors',
              sub === s.key
                ? 'bg-accent-soft font-medium text-ink'
                : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
            )}
          >
            {s.label}
          </button>
        ))}
      </nav>
      {/* Right content area */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {sub === 'overview' ? (
          <Overview />
        ) : sub === 'users' ? (
          <UsersPanel />
        ) : sub === 'tasks' ? (
          <TasksPanel onViewTrace={openTrace} />
        ) : sub === 'spaces' ? (
          <SpacesPanel />
        ) : sub === 'skills' ? (
          <BuiltinSkillsPanel />
        ) : sub === 'config' ? (
          <ConfigPanel />
        ) : (
          <TraceSubPanel initialId={traceId} />
        )}
      </div>
    </div>
  )
}
