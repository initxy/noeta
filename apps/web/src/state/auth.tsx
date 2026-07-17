import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { ApiError } from '../api/client'
import { authApi } from '../api/endpoints'
import type { User } from '../api/types'

interface AuthCtx {
  user: User | null
  /** false = still checking */
  checked: boolean
  devLoginEnabled: boolean
  login: (username: string) => Promise<void>
  logout: () => Promise<void>
}

const Ctx = createContext<AuthCtx | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [checked, setChecked] = useState(false)
  const [devLoginEnabled, setDevLoginEnabled] = useState(true)

  useEffect(() => {
    // Fetch me() and config() in parallel: config shapes the login page and is
    // independent of the login state.
    const meP = authApi
      .me()
      .then((r) => setUser(r.user))
      .catch(() => setUser(null))
    const cfgP = authApi
      .getConfig()
      .then((c) => {
        setDevLoginEnabled(c.dev_login_enabled)
      })
      .catch(() => {
        // Config fetch failed: conservatively keep dev-login available.
      })
    Promise.all([meP, cfgP]).finally(() => setChecked(true))
  }, [])

  const login = useCallback(async (username: string) => {
    const r = await authApi.devLogin(username)
    setUser(r.user)
  }, [])

  const logout = useCallback(async () => {
    try {
      await authApi.logout()
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
    }
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, checked, devLoginEnabled, login, logout }),
    [user, checked, devLoginEnabled, login, logout],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
