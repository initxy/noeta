import { useState } from 'react'
import { cn } from '../lib/cn'
import { useSpace } from '../state/space'
import { CreateSpaceModal } from './CreateSpaceModal'
import { IconChevron, IconPlus } from './icons'

/** Current space name + dropdown: switch space, create space, space settings (the modal is owned by Sidebar). */
export function SpaceSwitcher({ onOpenSettings }: { onOpenSettings: () => void }) {
  const { spaces, currentSpace, currentSpaceId, setCurrentSpace } = useSpace()
  const [open, setOpen] = useState(false)
  const [showCreate, setShowCreate] = useState(false)

  return (
    <div className="relative px-3 pt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-lg border border-border bg-bg px-2.5 py-2 text-left transition-colors hover:border-border-strong hover:bg-surface-2"
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent-soft text-[12px] font-semibold uppercase text-accent">
          {(currentSpace?.name || '?').charAt(0)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-ink">
            {currentSpace?.name ?? 'Select a space'}
          </span>
          {currentSpace && (
            <span className="block truncate text-[10.5px] text-ink-3">
              {currentSpace.is_personal
                ? 'Personal space'
                : `${currentSpace.member_count} member${currentSpace.member_count === 1 ? '' : 's'}`}
            </span>
          )}
        </span>
        <IconChevron className="h-4 w-4 shrink-0 text-ink-3" open={open} />
      </button>

      {open && (
        <>
          <button
            type="button"
            aria-label="Close"
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-30"
          />
          <div className="absolute left-3 right-3 z-40 mt-1 overflow-hidden rounded-lg border border-border bg-surface py-1 shadow-[var(--shadow)]">
            <ul className="max-h-64 overflow-y-auto">
              {spaces.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setCurrentSpace(s.id)
                      setOpen(false)
                    }}
                    className={cn(
                      'flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-surface-2',
                      s.id === currentSpaceId && 'bg-accent-soft',
                    )}
                  >
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-surface-2 text-[11px] font-semibold uppercase text-ink-2">
                      {s.name.charAt(0)}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink">
                      {s.name}
                    </span>
                    {s.is_personal && (
                      <span className="shrink-0 text-[10px] text-ink-3">Personal</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
            <div className="my-1 border-t border-border" />
            <button
              type="button"
              onClick={() => {
                setShowCreate(true)
                setOpen(false)
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px] text-ink-2 hover:bg-surface-2"
            >
              <IconPlus className="h-3.5 w-3.5" />
              Create space
            </button>
            {currentSpace && (
              <button
                type="button"
                onClick={() => {
                  onOpenSettings()
                  setOpen(false)
                }}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px] text-ink-2 hover:bg-surface-2"
              >
                <span className="h-3.5 w-3.5" aria-hidden />
                Space settings
              </button>
            )}
          </div>
        </>
      )}

      {showCreate && <CreateSpaceModal onClose={() => setShowCreate(false)} />}
    </div>
  )
}
