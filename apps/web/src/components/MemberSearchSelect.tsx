import { useEffect, useRef, useState } from 'react'
import { usersApi } from '../api/endpoints'
import type { User } from '../api/types'
import { cn } from '../lib/cn'
import { IconSearch } from './icons'

interface Props {
  /** identity accepts a username or an email (a value containing @ is treated as an email). */
  onAdd: (data: {
    username?: string
    email?: string
    role: 'owner' | 'member'
  }) => Promise<void> | void
  disabled?: boolean
}

/** Input + debounced /users/search autocomplete + role picker + add. */
export function MemberSearchSelect({ onAdd, disabled }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<User[]>([])
  const [open, setOpen] = useState(false)
  const [role, setRole] = useState<'owner' | 'member'>('member')
  const [submitting, setSubmitting] = useState(false)

  // Debounced search.
  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setResults([])
      return
    }
    const t = window.setTimeout(() => {
      usersApi
        .search(q, 8)
        .then((r) => setResults(r.users))
        .catch(() => setResults([]))
    }, 250)
    return () => window.clearTimeout(t)
  }, [query])

  const identityRef = useRef<HTMLInputElement>(null)

  const submit = async () => {
    const value = query.trim()
    if (!value || submitting) return
    setSubmitting(true)
    try {
      const data = value.includes('@')
        ? { email: value, role }
        : { username: value, role }
      await onAdd(data)
      setQuery('')
      setResults([])
      setOpen(false)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="relative">
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3">
            <IconSearch className="h-3.5 w-3.5" />
          </span>
          <input
            ref={identityRef}
            value={query}
            disabled={disabled}
            onChange={(e) => {
              setQuery(e.target.value)
              setOpen(true)
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => window.setTimeout(() => setOpen(false), 150)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submit()
            }}
            placeholder="username or email"
            className="w-full rounded-lg border border-border bg-bg py-1.5 pl-8 pr-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none disabled:opacity-50"
          />
          {open && results.length > 0 && (
            <ul className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-border bg-surface py-1 shadow-[var(--shadow)]">
              {results.map((u) => (
                <li key={u.username}>
                  <button
                    type="button"
                    // onMouseDown fires before the input's onBlur, so the option stays clickable.
                    onMouseDown={(e) => {
                      e.preventDefault()
                      setQuery(u.username)
                      setResults([])
                      setOpen(false)
                      identityRef.current?.focus()
                    }}
                    className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-surface-2"
                  >
                    {u.avatar ? (
                      <img
                        src={u.avatar}
                        alt=""
                        className="h-6 w-6 shrink-0 rounded-full object-cover"
                      />
                    ) : (
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[11px] font-medium uppercase text-ink">
                        {(u.name || u.username).charAt(0)}
                      </span>
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12.5px] text-ink">
                        {u.name || u.username}
                      </span>
                      <span className="block truncate text-[10.5px] text-ink-3">
                        {u.email || u.username}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <select
          value={role}
          disabled={disabled}
          onChange={(e) => setRole(e.target.value as 'owner' | 'member')}
          className="rounded-lg border border-border bg-bg px-2 text-[12.5px] text-ink focus:border-border-strong focus:outline-none disabled:opacity-50"
        >
          <option value="member">Member</option>
          <option value="owner">Owner</option>
        </select>
        <button
          type="button"
          disabled={disabled || submitting || !query.trim()}
          onClick={() => void submit()}
          className={cn(
            'shrink-0 rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-ink transition-opacity',
            'disabled:opacity-40',
          )}
        >
          Add
        </button>
      </div>
    </div>
  )
}
