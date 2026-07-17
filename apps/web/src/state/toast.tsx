import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

interface Toast {
  id: number
  text: string
  kind: 'error' | 'info'
}

interface ToastCtx {
  toast: (text: string, kind?: Toast['kind']) => void
}

const Ctx = createContext<ToastCtx | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const nextId = useRef(1)

  const toast = useCallback((text: string, kind: Toast['kind'] = 'error') => {
    const id = nextId.current++
    setItems((list) => [...list.slice(-3), { id, text, kind }])
    window.setTimeout(() => {
      setItems((list) => list.filter((t) => t.id !== id))
    }, 4200)
  }, [])

  const value = useMemo(() => ({ toast }), [toast])

  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 flex -translate-x-1/2 flex-col items-center gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`msg-enter pointer-events-auto max-w-md rounded-lg border px-4 py-2.5 text-[13px] shadow-[var(--shadow)] ${
              t.kind === 'error'
                ? 'border-danger/30 bg-danger-soft text-danger'
                : 'border-border bg-surface text-ink'
            }`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  )
}

export function useToast(): ToastCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useToast must be used inside ToastProvider')
  return ctx
}
