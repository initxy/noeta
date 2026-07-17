import { useState } from 'react'
import type { Channel, Session } from '../api/types'
import { cn } from '../lib/cn'
import { relativeTime } from '../lib/time'
import { useAuth } from '../state/auth'
import { useSpace } from '../state/space'
import { Logo } from './Logo'
import { SpaceSettingsPanel, type SpaceSettingsTab } from './SpaceSettingsPanel'
import { SpaceSwitcher } from './SpaceSwitcher'
import {
  IconBook,
  IconChat,
  IconFile,
  IconMemory,
  IconPlus,
  IconSettings,
  IconSidebar,
  IconSkill,
  IconThumbUp,
  IconTrash,
} from './icons'

export type MainView =
  | 'chat'
  | 'channel'
  | 'board'
  | 'skills'
  | 'templates'
  | 'knowledge'
  | 'memories'
  | 'feedback'
  | 'settings'
  | 'admin'

interface SidebarProps {
  sessions: Session[]
  loading: boolean
  activeId: string | null
  view: MainView
  running: boolean
  /** Collaboration area (channels / board): shown only in team spaces (ADR-0016 D1). */
  showCollab: boolean
  channels: Channel[]
  activeChannelId: string | null
  onSelectChannel: (id: string) => void
  onOpenBoard: () => void
  onCreateChannel: (name: string) => Promise<void>
  onViewChange: (view: MainView) => void
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onToggle: () => void
}

export function Sidebar({
  sessions,
  loading,
  activeId,
  view,
  running,
  showCollab,
  channels,
  activeChannelId,
  onSelectChannel,
  onOpenBoard,
  onCreateChannel,
  onViewChange,
  onSelect,
  onCreate,
  onDelete,
  onToggle,
}: SidebarProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [avatarError, setAvatarError] = useState(false)
  // Space settings modal (with skills / knowledge tabs).
  const [settingsTab, setSettingsTab] = useState<SpaceSettingsTab | null>(null)
  // Inline input for creating a channel.
  const [creatingChannel, setCreatingChannel] = useState(false)
  const [channelName, setChannelName] = useState('')
  const { user } = useAuth()
  const { currentSpace } = useSpace()
  const currentSpaceId = currentSpace?.id ?? null

  const collapseOnNarrow = () => {
    // Narrow-screen drawer: collapse the sidebar after a click, matching session selection.
    if (window.innerWidth < 1024) onToggle()
  }

  const submitChannel = async () => {
    const name = channelName.trim()
    if (!name) {
      setCreatingChannel(false)
      return
    }
    await onCreateChannel(name)
    setChannelName('')
    setCreatingChannel(false)
  }

  return (
    <div className="flex h-full w-64 flex-col border-r border-border bg-surface">
      {/* Top: brand area */}
      <div className="flex h-[52px] shrink-0 items-center gap-1.5 border-b border-border px-3">
        <button
          type="button"
          onClick={onToggle}
          title="Collapse session sidebar"
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconSidebar />
        </button>
        <Logo running={running} />
      </div>

      {/* Space switcher */}
      <SpaceSwitcher onOpenSettings={() => setSettingsTab('info')} />

      {/* Middle: collaboration area (channels / board, team spaces) + my sessions, one scroll container. */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {showCollab && (
          <>
            <div className="flex items-center justify-between px-3 pb-1 pt-3">
              <span className="text-[11px] font-medium uppercase tracking-wide text-ink-3">
                Channels
              </span>
              <button
                type="button"
                title="New channel"
                onClick={() => setCreatingChannel((v) => !v)}
                className="flex h-5 w-5 items-center justify-center rounded text-ink-3 hover:bg-surface-2 hover:text-ink"
              >
                <IconPlus className="h-3 w-3" />
              </button>
            </div>
            {creatingChannel && (
              <div className="px-3 pb-1">
                <input
                  autoFocus
                  value={channelName}
                  placeholder="Channel name; Enter to create"
                  onChange={(e) => setChannelName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                      void submitChannel()
                    } else if (e.key === 'Escape') {
                      setCreatingChannel(false)
                      setChannelName('')
                    }
                  }}
                  onBlur={() => void submitChannel()}
                  className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-border-strong"
                />
              </div>
            )}
            <ul className="space-y-0.5 px-2">
              {channels.length === 0 && !creatingChannel ? (
                <li className="px-3 py-1 text-[12px] text-ink-3">
                  No channels yet
                </li>
              ) : (
                channels.map((c) => (
                  <li key={c.id}>
                    <button
                      type="button"
                      onClick={() => {
                        onSelectChannel(c.id)
                        collapseOnNarrow()
                      }}
                      className={cn(
                        'flex w-full items-center gap-1.5 rounded-lg px-3 py-1.5 text-left text-[13px] transition-colors',
                        view === 'channel' && c.id === activeChannelId
                          ? 'bg-accent-soft font-medium text-ink'
                          : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
                      )}
                    >
                      <span className="font-mono text-[12px] text-ink-3">#</span>
                      <span className="min-w-0 flex-1 truncate">{c.name}</span>
                      {c.unread > 0 && (
                        <span className="shrink-0 rounded-full bg-accent px-1.5 py-0.5 font-mono text-[10px] leading-none text-white">
                          {c.unread > 99 ? '99+' : c.unread}
                        </span>
                      )}
                    </button>
                  </li>
                ))
              )}
            </ul>
            <div className="px-2 pt-1">
              <button
                type="button"
                onClick={() => {
                  onOpenBoard()
                  collapseOnNarrow()
                }}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-[13px] transition-colors',
                  view === 'board'
                    ? 'bg-accent-soft font-medium text-ink'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
                )}
              >
                <IconChat className="h-3.5 w-3.5 shrink-0" />
                Task board
              </button>
            </div>
            <div className="mx-3 mt-2 border-t border-border" />
          </>
        )}

        <div className="relative px-3 pb-2 pt-3">
          <div className="flex items-stretch gap-1.5">
            {/* Default click = an ordinary session (behavior unchanged). */}
            <button
              type="button"
              onClick={onCreate}
              className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-border bg-bg py-2 text-[13px] font-medium text-ink transition-colors hover:border-border-strong hover:bg-surface-2 disabled:opacity-50"
            >
              <IconPlus className="h-3.5 w-3.5" />
              New session
            </button>
          </div>
        </div>

        <div className="px-3 pb-1 pt-1">
          <span className="block truncate text-[11px] font-medium uppercase tracking-wide text-ink-3">
            My sessions
          </span>
        </div>

        <nav className="px-2 py-1" aria-label="Session list">
          {loading ? (
            <div className="space-y-2 px-1 pt-1">
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-12 animate-pulse rounded-lg bg-surface-2" />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <p className="px-3 pt-6 text-center text-[12.5px] leading-relaxed text-ink-3">
              No sessions yet.
              <br />
              Create one to start talking to the agent.
            </p>
          ) : (
            <ul className="space-y-0.5">
              {sessions.map((s) => (
                <li key={s.id} className="group relative">
                  <button
                    type="button"
                    onClick={() => onSelect(s.id)}
                    className={cn(
                      'w-full rounded-lg px-3 py-2 pr-9 text-left transition-colors',
                      view === 'chat' && s.id === activeId
                        ? 'bg-accent-soft'
                        : 'hover:bg-surface-2',
                    )}
                  >
                    <span
                      className={cn(
                        'block truncate text-[13px]',
                        s.id === activeId ? 'font-medium text-ink' : 'text-ink',
                      )}
                    >
                      {s.title || 'New session'}
                    </span>
                    <span className="mt-0.5 block font-mono text-[10.5px] text-ink-3">
                      {relativeTime(s.updated_at || s.created_at)}
                    </span>
                  </button>
                  {confirmId === s.id ? (
                    <button
                      type="button"
                      onClick={() => {
                        onDelete(s.id)
                        setConfirmId(null)
                      }}
                      onBlur={() => setConfirmId(null)}
                      autoFocus
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-md bg-danger px-1.5 py-1 text-[11px] font-medium text-white"
                    >
                      Confirm
                    </button>
                  ) : (
                    <button
                      type="button"
                      title="Delete session"
                      onClick={() => setConfirmId(s.id)}
                      className="absolute right-1.5 top-1/2 hidden h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-ink-3 hover:bg-surface-3 hover:text-danger group-hover:flex"
                    >
                      <IconTrash className="h-3.5 w-3.5" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </nav>
      </div>

      {/* Space configuration menu: skills / knowledge etc. (vertical full-row menu
          items, pinned to the bottom); "Admin console" is admin-only (user.is_admin),
          appended last. */}
      <div className="shrink-0 border-t border-border px-2 py-1.5">
        {(
          [
            { key: 'skills', label: 'Skills', Icon: IconSkill },
            { key: 'templates', label: 'Templates', Icon: IconFile },
            { key: 'knowledge', label: 'Knowledge base', Icon: IconBook },
            { key: 'memories', label: 'Memories', Icon: IconMemory },
            { key: 'feedback', label: 'Feedback', Icon: IconThumbUp },
            ...(user?.is_admin
              ? [{ key: 'admin', label: 'Admin console', Icon: IconSettings } as const]
              : []),
          ] as { key: MainView; label: string; Icon: typeof IconSkill }[]
        ).map(({ key, label, Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              onViewChange(key)
              collapseOnNarrow()
            }}
            className={cn(
              'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[12.5px] transition-colors',
              view === key
                ? 'bg-accent-soft font-medium text-ink'
                : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
            )}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" />
            {label}
          </button>
        ))}
      </div>

      {/* Bottom: personal settings entry (theme lives on the settings page). */}
      <div className="shrink-0 border-t border-border">
        <div className="flex items-center gap-1.5 px-2 py-2">
          <button
            type="button"
            onClick={() => {
              onViewChange('settings')
              collapseOnNarrow()
            }}
            title="Settings"
            className={cn(
              'flex min-w-0 flex-1 items-center gap-2 rounded-lg px-1.5 py-1 text-left transition-colors',
              view === 'settings'
                ? 'bg-accent-soft font-medium'
                : 'hover:bg-surface-2',
            )}
          >
            {user?.avatar && !avatarError ? (
              <img
                src={user.avatar}
                alt={user.name || user.username}
                onError={() => setAvatarError(true)}
                className="h-7 w-7 shrink-0 rounded-full object-cover"
              />
            ) : (
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[12px] font-medium uppercase text-ink">
                {(user?.name || user?.username || '?').charAt(0)}
              </span>
            )}
            <div className="flex min-w-0 flex-1 flex-col leading-tight">
              <span
                className="truncate text-[12.5px] text-ink"
                title={user?.name || user?.username}
              >
                {user?.name || user?.username}
              </span>
              {user?.name && user?.username ? (
                <span
                  className="truncate text-[10.5px] text-ink-3"
                  title={user.username}
                >
                  {user.username}
                </span>
              ) : null}
            </div>
          </button>
        </div>
      </div>

      {settingsTab && currentSpaceId && (
        <SpaceSettingsPanel
          key={settingsTab}
          spaceId={currentSpaceId}
          initialTab={settingsTab}
          onClose={() => setSettingsTab(null)}
        />
      )}
    </div>
  )
}
