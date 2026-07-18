import { useCallback, useEffect, useState } from 'react'

/**
 * Send-shortcut mode:
 * - `enter`: Enter sends, Shift+Enter inserts a newline (default, legacy-compatible).
 * - `mod-enter`: Cmd/Ctrl+Enter sends, Enter inserts a newline — avoids
 *   accidentally sending a half-written message with Enter.
 */
export type SendMode = 'enter' | 'mod-enter'

const STORAGE_KEY = 'noeta-send-mode'

function load(): SendMode {
  return localStorage.getItem(STORAGE_KEY) === 'mod-enter' ? 'mod-enter' : 'enter'
}

const isMac =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** Display name of the modifier key: ⌘ on macOS, Ctrl elsewhere. */
export const MOD_KEY_LABEL = isMac ? '⌘' : 'Ctrl'

/** Hint text of the send shortcut for the current mode, e.g. `Enter` / `⌘+Enter`. */
export function sendKeyHint(mode: SendMode): string {
  return mode === 'enter' ? 'Enter' : `${MOD_KEY_LABEL}+Enter`
}

/**
 * Send-shortcut preference: persisted in localStorage. The Composer and the
 * personal settings page are never mounted at the same time, so no shared
 * Provider is needed; still listens to the storage event to stay consistent
 * across tabs.
 */
export function useSendMode(): [SendMode, (m: SendMode) => void] {
  const [mode, setMode] = useState<SendMode>(load)

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setMode(load())
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const set = useCallback((m: SendMode) => {
    localStorage.setItem(STORAGE_KEY, m)
    setMode(m)
  }, [])

  return [mode, set]
}
