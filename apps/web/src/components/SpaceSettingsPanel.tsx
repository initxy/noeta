import { useCallback, useEffect, useState } from 'react'
import {
  agentConfigApi,
  knowledgeApi,
  modelsApi,
  spacesApi,
} from '../api/endpoints'
import type {
  AgentConfig,
  KnowledgeSource,
  ModelInfo,
  SpaceDetail,
  SpaceMember,
} from '../api/types'
import { cn } from '../lib/cn'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import { McpConnectorsTab } from './McpConnectorsTab'
import { MemberSearchSelect } from './MemberSearchSelect'
import {
  IconClose,
  IconGlobe,
  IconSettings,
  IconSkill,
  IconTrash,
  IconUsers,
} from './icons'

export type SpaceSettingsTab = 'info' | 'members' | 'agent' | 'connectors'

interface Props {
  spaceId: string
  initialTab?: SpaceSettingsTab
  onClose: () => void
}

/** Space settings: tabbed (info / members / agent config); only owners can manage.
 * Skills / knowledge configuration moved out to full pages (Sidebar bottom menu). */
export function SpaceSettingsPanel({ spaceId, initialTab, onClose }: Props) {
  const { refreshSpaces, setCurrentSpace } = useSpace()
  const { toast } = useToast()
  const [detail, setDetail] = useState<SpaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<SpaceSettingsTab>(initialTab ?? 'info')
  const [confirmDelete, setConfirmDelete] = useState(false)

  const reload = useCallback(async () => {
    const r = await spacesApi.get(spaceId)
    setDetail(r.space)
    return r.space
  }, [spaceId])

  useEffect(() => {
    reload()
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load space'))
      .finally(() => setLoading(false))
  }, [reload, toast])

  const isOwner = detail?.my_role === 'owner'
  const isPersonal = detail?.is_personal ?? false
  const canManage = isOwner && !isPersonal

  const deleteSpace = async () => {
    try {
      await spacesApi.remove(spaceId)
      const list = await refreshSpaces()
      const personal = list.find((s) => s.is_personal)
      if (personal) setCurrentSpace(personal.id)
      onClose()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to delete space')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/40"
      />
      <div className="msg-enter relative flex max-h-[85vh] w-full max-w-lg flex-col rounded-xl border border-border bg-surface shadow-[var(--shadow)]">
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-[15px] font-semibold text-ink">Space settings</h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>

        {/* Tab navigation */}
        <div className="flex shrink-0 gap-1 border-b border-border px-3 py-1.5">
          <TabBtn active={tab === 'info'} onClick={() => setTab('info')} icon={<IconSettings className="h-3.5 w-3.5" />}>
            Info
          </TabBtn>
          <TabBtn active={tab === 'members'} onClick={() => setTab('members')} icon={<IconUsers className="h-3.5 w-3.5" />}>
            Members{detail ? ` · ${detail.members.length}` : ''}
          </TabBtn>
          <TabBtn active={tab === 'agent'} onClick={() => setTab('agent')} icon={<IconSkill className="h-3.5 w-3.5" />}>
            Agent config
          </TabBtn>
          <TabBtn active={tab === 'connectors'} onClick={() => setTab('connectors')} icon={<IconGlobe className="h-3.5 w-3.5" />}>
            Connectors
          </TabBtn>
        </div>

        {loading || !detail ? (
          <div className="p-6">
            <div className="h-24 animate-pulse rounded-lg bg-surface-2" />
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {tab === 'info' && (
              <InfoTab
                detail={detail}
                isOwner={isOwner}
                isPersonal={isPersonal}
                canManage={canManage}
                onReload={reload}
                onRefreshSpaces={refreshSpaces}
                confirmDelete={confirmDelete}
                setConfirmDelete={setConfirmDelete}
                onDelete={deleteSpace}
              />
            )}
            {tab === 'members' && (
              <MembersTab
                detail={detail}
                canManage={canManage}
                onReload={reload}
                onRefreshSpaces={refreshSpaces}
              />
            )}
            {tab === 'agent' && (
              <AgentConfigTab spaceId={detail.id} isOwner={isOwner} />
            )}
            {tab === 'connectors' && (
              <McpConnectorsTab spaceId={detail.id} isOwner={isOwner} />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ tab btn

function TabBtn({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[12px] transition-colors',
        active
          ? 'bg-surface-2 text-ink'
          : 'text-ink-3 hover:bg-surface-2 hover:text-ink-2',
      )}
    >
      {icon}
      {children}
    </button>
  )
}

// ------------------------------------------------------------------ info tab

interface InfoTabProps {
  detail: SpaceDetail
  isOwner: boolean
  isPersonal: boolean
  canManage: boolean
  onReload: () => Promise<SpaceDetail>
  onRefreshSpaces: () => Promise<unknown>
  confirmDelete: boolean
  setConfirmDelete: (v: boolean) => void
  onDelete: () => void
}

function InfoTab({
  detail,
  isOwner,
  isPersonal,
  canManage,
  onReload,
  onRefreshSpaces,
  confirmDelete,
  setConfirmDelete,
  onDelete,
}: InfoTabProps) {
  const { toast } = useToast()
  const [name, setName] = useState(detail.name)
  const [description, setDescription] = useState(detail.description)
  const [savingInfo, setSavingInfo] = useState(false)

  // Sync once the detail loads.
  useEffect(() => {
    setName(detail.name)
    setDescription(detail.description)
  }, [detail.name, detail.description])

  const saveInfo = async () => {
    if (savingInfo) return
    const nextName = name.trim()
    if (!nextName) return
    setSavingInfo(true)
    try {
      await spacesApi.update(detail.id, {
        name: nextName,
        description: description.trim(),
      })
      await onReload()
      await onRefreshSpaces()
      toast('Saved', 'info')
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSavingInfo(false)
    }
  }

  const dirty = name.trim() !== detail.name || description.trim() !== detail.description

  return (
    <>
      <section className="mb-5">
        <label className="mb-1 block text-[12px] text-ink-2">
          Space name{isPersonal && <span className="text-ink-3"> (a personal space cannot be renamed)</span>}
        </label>
        <input
          value={name}
          disabled={!isOwner || isPersonal}
          maxLength={64}
          onChange={(e) => setName(e.target.value)}
          className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink focus:border-border-strong focus:outline-none disabled:opacity-60"
        />
        <label className="mb-1 block text-[12px] text-ink-2">Description</label>
        <textarea
          value={description}
          disabled={!isOwner}
          maxLength={500}
          rows={2}
          onChange={(e) => setDescription(e.target.value)}
          className="w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink focus:border-border-strong focus:outline-none disabled:opacity-60"
        />
        {isOwner && (
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              disabled={!dirty || savingInfo || !name.trim()}
              onClick={() => void saveInfo()}
              className="rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-ink disabled:opacity-40"
            >
              Save
            </button>
          </div>
        )}
      </section>

      {/* Delete team space */}
      {canManage && (
        <section className="mt-6 border-t border-border pt-4">
          {confirmDelete ? (
            <div className="flex items-center justify-between gap-3">
              <span className="text-[12.5px] text-ink-2">
                Deleting also removes the space's sessions. This cannot be undone.
              </span>
              <div className="flex shrink-0 gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmDelete(false)}
                  className="rounded-lg border border-border px-2.5 py-1.5 text-[12.5px] text-ink-2 hover:bg-surface-2"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void onDelete()}
                  className="rounded-lg bg-danger px-2.5 py-1.5 text-[12.5px] font-medium text-white"
                >
                  Confirm delete
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className="text-[12.5px] text-danger hover:underline"
            >
              Delete space
            </button>
          )}
        </section>
      )}
    </>
  )
}

// ------------------------------------------------------------------ members tab

interface MembersTabProps {
  detail: SpaceDetail
  canManage: boolean
  onReload: () => Promise<SpaceDetail>
  onRefreshSpaces: () => Promise<unknown>
}

function MembersTab({ detail, canManage, onReload, onRefreshSpaces }: MembersTabProps) {
  const { toast } = useToast()

  const changeRole = async (member: SpaceMember, role: 'owner' | 'member') => {
    try {
      await spacesApi.updateMemberRole(detail.id, member.username, role)
      await onReload()
      await onRefreshSpaces()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to change role')
    }
  }

  const removeMember = async (member: SpaceMember) => {
    try {
      await spacesApi.removeMember(detail.id, member.username)
      await onReload()
      await onRefreshSpaces()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to remove member')
    }
  }

  const addMember = async (data: {
    username?: string
    email?: string
    role: 'owner' | 'member'
  }) => {
    try {
      await spacesApi.addMember(detail.id, data)
      await onReload()
      await onRefreshSpaces()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to add member')
    }
  }

  return (
    <section>
      {canManage && (
        <div className="mb-3">
          <MemberSearchSelect onAdd={addMember} />
        </div>
      )}
      <ul className="space-y-1">
        {detail.members.map((m) => (
          <li
            key={m.username}
            className="flex items-center gap-2.5 rounded-lg px-1.5 py-1.5 hover:bg-surface-2"
          >
            {m.avatar ? (
              <img
                src={m.avatar}
                alt=""
                className="h-8 w-8 shrink-0 rounded-full object-cover"
              />
            ) : (
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[13px] font-medium uppercase text-ink">
                {(m.name || m.username).charAt(0)}
              </span>
            )}
            <div className="min-w-0 flex-1">
              <span className="block truncate text-[13px] text-ink">
                {m.name || m.username}
              </span>
              <span className="block truncate text-[11px] text-ink-3">
                {m.email || m.username}
              </span>
            </div>
            {canManage ? (
              <select
                value={m.role}
                onChange={(e) =>
                  void changeRole(m, e.target.value as 'owner' | 'member')
                }
                className="rounded-md border border-border bg-bg px-1.5 py-1 text-[11.5px] text-ink-2 focus:border-border-strong focus:outline-none"
              >
                <option value="member">Member</option>
                <option value="owner">Owner</option>
              </select>
            ) : (
              <span
                className={cn(
                  'rounded-md px-1.5 py-0.5 text-[11px]',
                  m.role === 'owner'
                    ? 'bg-accent-soft text-accent'
                    : 'bg-surface-2 text-ink-3',
                )}
              >
                {m.role === 'owner' ? 'Owner' : 'Member'}
              </span>
            )}
            {canManage && (
              <button
                type="button"
                title="Remove member"
                onClick={() => void removeMember(m)}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-surface-3 hover:text-danger"
              >
                <IconTrash className="h-3.5 w-3.5" />
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

// ------------------------------------------------------------------ agent config tab

const EFFORT_OPTIONS = [
  { value: '', label: 'Platform default' },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
]

function AgentConfigTab({ spaceId, isOwner }: { spaceId: string; isOwner: boolean }) {
  const { toast } = useToast()
  const [config, setConfig] = useState<AgentConfig | null>(null)
  const [models, setModels] = useState<ModelInfo[]>([])
  const [sources, setSources] = useState<KnowledgeSource[]>([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    Promise.all([
      agentConfigApi.get(spaceId),
      modelsApi.list(),
      knowledgeApi.list(spaceId),
    ])
      .then(([c, m, k]) => {
        setConfig(c.config)
        setModels(m.models)
        setSources(k.sources)
      })
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load agent config'))
  }, [spaceId, toast])

  if (!config) {
    return <div className="h-24 animate-pulse rounded-lg bg-surface-2" />
  }

  const allSources = config.knowledge_sources === null

  const save = async () => {
    if (saving) return
    setSaving(true)
    try {
      const r = await agentConfigApi.update(spaceId, {
        prompt: config.prompt,
        memory_enabled: config.memory_enabled,
        default_model: config.default_model,
        default_effort: config.default_effort,
        ...(config.knowledge_sources === null
          ? { clear_knowledge_sources: true }
          : { knowledge_sources: config.knowledge_sources }),
      })
      setConfig(r.config)
      toast('Saved', 'info')
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <section className="mb-4">
        <label className="mb-1 block text-[12px] text-ink-2">
          Prompt (appended segment)
          <span className="text-ink-3">: appended after the platform base prompt to customize this space's agent persona and habits</span>
        </label>
        <textarea
          value={config.prompt}
          disabled={!isOwner}
          maxLength={4000}
          rows={5}
          placeholder="e.g. You are this team's analytics expert, fluent in our domain terms; check the knowledge base before answering metric questions…"
          onChange={(e) => setConfig({ ...config, prompt: e.target.value })}
          className="w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink focus:border-border-strong focus:outline-none disabled:opacity-60"
        />
      </section>

      <section className="mb-4 grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-[12px] text-ink-2">Default model</label>
          <select
            value={config.default_model}
            disabled={!isOwner}
            onChange={(e) => setConfig({ ...config, default_model: e.target.value })}
            className="w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-[12.5px] text-ink focus:outline-none disabled:opacity-60"
          >
            <option value="">Platform default</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-[12px] text-ink-2">Default reasoning effort</label>
          <select
            value={config.default_effort}
            disabled={!isOwner}
            onChange={(e) => setConfig({ ...config, default_effort: e.target.value })}
            className="w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-[12.5px] text-ink focus:outline-none disabled:opacity-60"
          >
            {EFFORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
      </section>

      <section className="mb-4 space-y-2">
        <ToggleRow
          label="Memory"
          hint="Takes effect once the memory feature ships; when on, sessions can store and recall this space's memory"
          checked={config.memory_enabled}
          disabled={!isOwner}
          onChange={(v) => setConfig({ ...config, memory_enabled: v })}
        />
      </section>

      <section className="mb-4">
        <label className="mb-1.5 block text-[12px] text-ink-2">Knowledge sources used in sessions</label>
        <ToggleRow
          label="All knowledge sources"
          hint="Newly added sources join automatically"
          checked={allSources}
          disabled={!isOwner}
          onChange={(v) =>
            setConfig({
              ...config,
              knowledge_sources: v ? null : sources.map((s) => s.id),
            })
          }
        />
        {!allSources && (
          <ul className="mt-2 space-y-1 rounded-lg border border-border p-2">
            {sources.length === 0 && (
              <li className="px-1 py-0.5 text-[12px] text-ink-3">This space has no knowledge sources yet</li>
            )}
            {sources.map((s) => {
              const selected = config.knowledge_sources?.includes(s.id) ?? false
              return (
                <li key={s.id} className="flex items-center gap-2 px-1 py-0.5">
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!isOwner}
                    onChange={(e) => {
                      const cur = config.knowledge_sources ?? []
                      setConfig({
                        ...config,
                        knowledge_sources: e.target.checked
                          ? [...cur, s.id]
                          : cur.filter((id) => id !== s.id),
                      })
                    }}
                    className="h-3.5 w-3.5 accent-[var(--accent)]"
                  />
                  <span className="text-[12.5px] text-ink">{s.name}</span>
                  {s.status !== 'ready' && (
                    <span className="text-[11px] text-ink-3">({s.status}; not mounted until ready)</span>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {isOwner && (
        <div className="flex justify-end">
          <button
            type="button"
            disabled={saving}
            onClick={() => void save()}
            className="rounded-lg bg-accent px-3 py-1.5 text-[12.5px] font-medium text-accent-ink disabled:opacity-40"
          >
            Save
          </button>
        </div>
      )}
    </>
  )
}

function ToggleRow({
  label,
  hint,
  checked,
  disabled,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  disabled?: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-3 rounded-lg border border-border px-3 py-2">
      <span className="min-w-0">
        <span className="block text-[12.5px] text-ink">{label}</span>
        {hint && <span className="block text-[11.5px] text-ink-3">{hint}</span>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 shrink-0 accent-[var(--accent)]"
      />
    </label>
  )
}
