import { useState } from 'react'
import { cn } from '../lib/cn'
import { useAuth } from '../state/auth'
import { MOD_KEY_LABEL, useSendMode } from '../state/sendMode'
import { useTheme } from '../state/theme'
import { IconLogout, IconMoon, IconSun } from './icons'

/**
 * User settings page: header + account info + appearance + chat preferences.
 * Layout matches SkillsPage (min-h-0 flex-1 overflow-y-auto + mx-auto max-w-3xl
 * centered container).
 */
export function UserSettingsPage() {
  const { user, logout } = useAuth()
  const { mode, toggle } = useTheme()
  const [sendMode, setSendMode] = useSendMode()
  const [avatarError, setAvatarError] = useState(false)

  return (
    <div className="flex h-full w-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-8">
          {/* Header (left-aligned, sharing the content area's left edge for a
              consistent settings-page look). */}
          <h1 className="text-[20px] font-semibold text-ink">Settings</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
            Manage your account and preferences
          </p>

          {/* Account info */}
          <section className="mt-6 flex items-center gap-4 rounded-lg border border-border bg-surface px-5 py-4">
            {user?.avatar && !avatarError ? (
              <img
                src={user.avatar}
                alt={user.name || user.username}
                onError={() => setAvatarError(true)}
                className="h-14 w-14 shrink-0 rounded-full object-cover"
              />
            ) : (
              <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[20px] font-medium uppercase text-ink">
                {(user?.name || user?.username || '?').charAt(0)}
              </span>
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate text-[15px] font-medium text-ink">
                {user?.name || user?.username}
              </p>
              <p className="truncate text-[12.5px] text-ink-3">
                {user?.email || user?.username}
              </p>
            </div>
            <button
              type="button"
              onClick={() => void logout()}
              className="flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] text-ink-2 transition-colors hover:border-border-strong hover:bg-surface-2 hover:text-danger"
            >
              <IconLogout className="h-3.5 w-3.5" />
              Sign out
            </button>
          </section>

          {/* Appearance */}
          <section className="mt-4 rounded-lg border border-border bg-surface">
            <div className="border-b border-border px-4 py-2.5">
              <span className="text-[13px] font-medium text-ink-2">Appearance</span>
            </div>
            <div className="flex items-center gap-2.5 px-4 py-2.5">
              <span className="min-w-0 flex-1 text-[13px] text-ink-2">Theme</span>
              <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-border p-0.5">
                {(
                  [
                    { key: 'light', label: 'Light', Icon: IconSun },
                    { key: 'dark', label: 'Dark', Icon: IconMoon },
                  ] as const
                ).map(({ key, label, Icon }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => {
                      if (mode !== key) toggle()
                    }}
                    className={cn(
                      'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12.5px] transition-colors',
                      mode === key
                        ? 'bg-accent-soft font-medium text-ink'
                        : 'text-ink-3 hover:text-ink',
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </section>

          {/* Chat */}
          <section className="mt-4 rounded-lg border border-border bg-surface">
            <div className="border-b border-border px-4 py-2.5">
              <span className="text-[13px] font-medium text-ink-2">Chat</span>
            </div>
            <div className="flex items-center gap-2.5 px-4 py-2.5">
              <div className="min-w-0 flex-1">
                <span className="block text-[13px] text-ink-2">Send shortcut</span>
                <span className="mt-0.5 block text-[11.5px] leading-snug text-ink-3">
                  Pick "{MOD_KEY_LABEL}+Enter to send" to avoid sending while composing
                  text or on an accidental Enter
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-border p-0.5">
                {(
                  [
                    { key: 'enter', label: 'Enter to send' },
                    { key: 'mod-enter', label: `${MOD_KEY_LABEL}+Enter to send` },
                  ] as const
                ).map(({ key, label }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setSendMode(key)}
                    className={cn(
                      'rounded-md px-2.5 py-1 text-[12.5px] transition-colors',
                      sendMode === key
                        ? 'bg-accent-soft font-medium text-ink'
                        : 'text-ink-3 hover:text-ink',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
