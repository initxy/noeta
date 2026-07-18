import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { spacesApi } from '../api/endpoints'
import type { Space } from '../api/types'

interface SpaceCtx {
  spaces: Space[]
  currentSpaceId: string | null
  currentSpace: Space | null
  loading: boolean
  setCurrentSpace: (id: string) => void
  refreshSpaces: () => Promise<Space[]>
  createSpace: (name: string, description?: string) => Promise<Space>
}

const Ctx = createContext<SpaceCtx | null>(null)
const STORAGE_KEY = 'noeta-space-id'

export function SpaceProvider({ children }: { children: ReactNode }) {
  const [spaces, setSpaces] = useState<Space[]>([])
  const [currentSpaceId, setCurrentSpaceId] = useState<string | null>(() =>
    localStorage.getItem(STORAGE_KEY),
  )
  const [loading, setLoading] = useState(true)

  const refreshSpaces = useCallback(async () => {
    const r = await spacesApi.list()
    setSpaces(r.spaces)
    return r.spaces
  }, [])

  // Initial fetch: restore currentSpaceId from localStorage; fall back to the
  // personal space when it is invalid.
  useEffect(() => {
    refreshSpaces()
      .then((list) => {
        setCurrentSpaceId((cur) => {
          if (cur && list.some((s) => s.id === cur)) return cur
          const personal = list.find((s) => s.is_personal)
          return personal?.id ?? list[0]?.id ?? null
        })
      })
      .catch(() => {
        /* Fetch failed: still end loading; the UI degrades to a no-space state. */
      })
      .finally(() => setLoading(false))
  }, [refreshSpaces])

  // Persist the current space so it survives a page refresh.
  useEffect(() => {
    if (currentSpaceId) localStorage.setItem(STORAGE_KEY, currentSpaceId)
  }, [currentSpaceId])

  const setCurrentSpace = useCallback((id: string) => setCurrentSpaceId(id), [])

  const createSpace = useCallback(
    async (name: string, description?: string) => {
      const r = await spacesApi.create({ name, description })
      await refreshSpaces()
      setCurrentSpaceId(r.space.id)
      return r.space
    },
    [refreshSpaces],
  )

  const currentSpace = useMemo(
    () => spaces.find((s) => s.id === currentSpaceId) ?? null,
    [spaces, currentSpaceId],
  )

  const value = useMemo(
    () => ({
      spaces,
      currentSpaceId,
      currentSpace,
      loading,
      setCurrentSpace,
      refreshSpaces,
      createSpace,
    }),
    [spaces, currentSpaceId, currentSpace, loading, setCurrentSpace, refreshSpaces, createSpace],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useSpace(): SpaceCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useSpace must be used inside SpaceProvider')
  return ctx
}
