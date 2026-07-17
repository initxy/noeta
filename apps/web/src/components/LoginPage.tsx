import { useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { useAuth } from '../state/auth'
import { Logo } from './Logo'
import { ThemeToggle } from './ThemeToggle'

export function LoginPage() {
  const { login, devLoginEnabled } = useAuth()
  const [username, setUsername] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const name = username.trim()
    if (!name || busy) return
    setBusy(true)
    setError(null)
    try {
      await login(name)
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError('Dev login is disabled on this deployment.')
      } else {
        setError(err instanceof Error ? err.message : 'Login failed')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative flex h-full flex-col items-center justify-center px-6">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>
      <div className="msg-enter w-full max-w-sm">
        <div className="mb-2 flex justify-center">
          <Logo size="lg" />
        </div>
        <p className="mb-10 text-center text-[13px] text-ink-3">
          Noeta Agent · your team's agent workbench
        </p>
        {devLoginEnabled ? (
          <form
            onSubmit={submit}
            className="rounded-2xl border border-border bg-surface p-6 shadow-[var(--shadow)]"
          >
            <label
              htmlFor="dev-username"
              className="mb-2 block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3"
            >
              dev login
            </label>
            <input
              id="dev-username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="Enter a username to open the local dev environment"
              className="w-full rounded-lg border border-border bg-bg px-3.5 py-2.5 text-[14px] text-ink outline-none transition-colors placeholder:text-ink-3 focus:border-accent"
            />
            {error && (
              <p role="alert" className="mt-3 text-[12.5px] leading-relaxed text-danger">
                {error}
              </p>
            )}
            <button
              type="submit"
              disabled={!username.trim() || busy}
              className="mt-4 w-full rounded-lg bg-accent py-2.5 text-[14px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? 'Signing in…' : 'Enter the workbench'}
            </button>
            <p className="mt-4 text-center text-[12px] leading-relaxed text-ink-3">
              Local development login; the identity only isolates your session data.
            </p>
          </form>
        ) : (
          <div className="rounded-2xl border border-border bg-surface p-6 shadow-[var(--shadow)]">
            <p className="text-center text-[14px] leading-relaxed text-ink-2">
              Login is disabled on this deployment. Contact your administrator.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
