import { useCallback, useEffect, useState } from 'react'
import { sessionsApi } from '../api/endpoints'
import type { SandboxPreview } from '../api/types'
import { cn } from '../lib/cn'
import { FilesPanel, type FileOpenRequest } from './FilesPanel'
import {
  IconCode,
  IconFile,
  IconGlobe,
  IconPanel,
  IconTerminal,
} from './icons'

const WIDTH_KEY = 'noeta-panel-width'
const COLLAPSED_KEY = 'noeta-panel-collapsed'
const MIN_WIDTH = 280

/** Id of the fixed tab; not closable. */
const FILES_TAB = 'files'

/** Sandbox live-preview tabs (appear when the session has a live container).
 * The iframe points at an isolated preview origin (same hostname + the port
 * from the discovery endpoint) — the panels need allow-same-origin (noVNC
 * localStorage / code-server service worker), and mounting them on the main
 * origin would hand cookies / the API surface to container content, so when
 * the port is missing we rather show nothing than fall back to the main port. */
const PREVIEW_TABS = [
  {
    id: 'sandbox:browser',
    key: 'browser',
    label: 'Browser',
    Icon: IconGlobe,
    sandboxAttrs:
      'allow-scripts allow-forms allow-modals allow-popups allow-same-origin',
  },
  {
    id: 'sandbox:terminal',
    key: 'terminal',
    label: 'Terminal',
    Icon: IconTerminal,
    sandboxAttrs: 'allow-scripts allow-forms allow-modals allow-same-origin',
  },
  {
    id: 'sandbox:code',
    key: 'code',
    label: 'Editor',
    Icon: IconCode,
    sandboxAttrs:
      'allow-scripts allow-forms allow-modals allow-popups allow-same-origin allow-downloads',
  },
] as const

/** Sandbox-preview discovery: re-queried on session switch / every turn end
 * (the container is only allocated on the first turn; 404 = no preview). */
function useSandboxPreview(
  sessionId: string,
  refreshKey: number,
): SandboxPreview | null {
  const [preview, setPreview] = useState<SandboxPreview | null>(null)
  // Clear immediately on session switch (without waiting for the new request,
  // avoiding a brief cross-session leak); refreshKey only re-queries, no clear
  useEffect(() => setPreview(null), [sessionId])
  useEffect(() => {
    let cancelled = false
    sessionsApi
      .preview(sessionId)
      .then((p) => {
        if (!cancelled) setPreview(p.port != null ? p : null)
      })
      .catch(() => {
        if (!cancelled) setPreview(null)
      })
    return () => {
      cancelled = true
    }
  }, [sessionId, refreshKey])
  return preview
}

function maxWidth(): number {
  return Math.round(window.innerWidth * 0.6)
}

function clampWidth(w: number): number {
  return Math.min(Math.max(w, MIN_WIDTH), maxWidth())
}

/**
 * Side-panel state: active tab / width / collapsed, with width and collapsed
 * persisted to localStorage.
 * openDoc is called from citation origin links in the conversation flow: the
 * vendor doc-preview tabs were dropped in this port, so it opens the URL in a
 * new browser tab (the interface is kept for App.tsx's plumbing).
 * openWorkspaceFile is called from workspace-file chips in the conversation
 * flow: it activates the "Workspace files" tab, expands the panel, and hands
 * FilesPanel an open request (nonce-deduped so the same file can be clicked
 * repeatedly).
 */
export function useSidePanel() {
  const [activeTab, setActiveTab] = useState<string>(FILES_TAB)
  const [fileRequest, setFileRequest] = useState<FileOpenRequest | null>(null)
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSED_KEY) === '1',
  )
  const [width, setWidth] = useState(() =>
    clampWidth(Number(localStorage.getItem(WIDTH_KEY)) || 320),
  )

  // Persistence: written to localStorage alongside the state (side effects stay out of setState updaters)
  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0')
  }, [collapsed])
  useEffect(() => {
    localStorage.setItem(WIDTH_KEY, String(width))
  }, [width])

  const openDoc = useCallback((url: string) => {
    window.open(url, '_blank', 'noreferrer')
  }, [])

  const openWorkspaceFile = useCallback((path: string) => {
    setFileRequest((prev) => ({ path, nonce: (prev?.nonce ?? 0) + 1 }))
    setActiveTab(FILES_TAB)
    setCollapsed(false)
  }, [])

  const clearFileRequest = useCallback(() => setFileRequest(null), [])

  const toggleCollapsed = useCallback(() => setCollapsed((v) => !v), [])

  const setPanelWidth = useCallback((w: number) => setWidth(clampWidth(w)), [])

  return {
    activeTab,
    setActiveTab,
    collapsed,
    width,
    fileRequest,
    openDoc,
    openWorkspaceFile,
    clearFileRequest,
    toggleCollapsed,
    setPanelWidth,
  }
}

export type SidePanelState = ReturnType<typeof useSidePanel>

interface SidePanelProps {
  sessionId: string
  refreshKey: number
  panel: SidePanelState
}

/** Right-hand multi-tab panel: the fixed "Workspace files" tab + sandbox preview tabs; the left edge drags to resize. */
export function SidePanel({ sessionId, refreshKey, panel }: SidePanelProps) {
  const {
    activeTab,
    setActiveTab,
    collapsed,
    width,
    fileRequest,
    clearFileRequest,
    toggleCollapsed,
    setPanelWidth,
  } = panel
  const [dragging, setDragging] = useState(false)
  const preview = useSandboxPreview(sessionId, refreshKey)
  // Preview iframes mount lazily: loaded on first open (neither noVNC nor
  // code-server is light), then kept mounted hidden to preserve connection
  // state. Cleared on session switch (a new session's panels reload on demand).
  const [openedPanels, setOpenedPanels] = useState<Set<string>>(new Set())
  useEffect(() => setOpenedPanels(new Set()), [sessionId])
  // Container gone (preview 404) while a preview tab is active: fall back to the files tab
  useEffect(() => {
    if (preview == null && activeTab.startsWith('sandbox:')) {
      setActiveTab(FILES_TAB)
    }
  }, [preview, activeTab, setActiveTab])
  const previewBase = preview
    ? `${window.location.protocol}//${window.location.hostname}:${preview.port}` +
      `/sandbox-preview/${encodeURIComponent(preview.token)}/`
    : ''

  // Drag listeners live on window, driven by an effect: both pointerup and unmount clean up
  useEffect(() => {
    if (!dragging) return
    const onMove = (ev: PointerEvent) => {
      setPanelWidth(window.innerWidth - ev.clientX)
    }
    const onUp = () => setDragging(false)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [dragging, setPanelWidth])

  if (collapsed) {
    return (
      <div className="flex h-full w-10 shrink-0 flex-col border-l border-border bg-surface">
        {/* Top 52px area keeps a border-b: the three columns' top-bar line stays continuous while collapsed */}
        <div className="flex h-[52px] shrink-0 items-center justify-center border-b border-border">
          <button
            type="button"
            onClick={toggleCollapsed}
            title="Expand side panel"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
          >
            <IconPanel />
          </button>
        </div>
      </div>
    )
  }

  return (
    <div
      style={{ width }}
      className="relative flex h-full shrink-0 flex-col border-l border-border bg-surface"
    >
      {/* Left-edge drag handle */}
      <div
        role="separator"
        aria-orientation="vertical"
        onPointerDown={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        className={cn(
          'absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize',
          'hover:bg-accent/25',
          dragging && 'bg-accent/25',
        )}
      />

      {/* Tab bar: 52px tall, aligned with the sidebar / main-column top bars */}
      <div className="flex h-[52px] shrink-0 items-center gap-0.5 border-b border-border px-1.5">
        <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
          <button
            type="button"
            onClick={() => setActiveTab(FILES_TAB)}
            className={cn(
              'flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] transition-colors',
              activeTab === FILES_TAB
                ? 'bg-accent-soft font-medium text-ink'
                : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
            )}
          >
            <IconFile className="h-3 w-3" />
            Workspace files
          </button>
          {preview != null &&
            PREVIEW_TABS.map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => {
                  setOpenedPanels((s) => (s.has(id) ? s : new Set(s).add(id)))
                  setActiveTab(id)
                }}
                className={cn(
                  'flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] transition-colors',
                  activeTab === id
                    ? 'bg-accent-soft font-medium text-ink'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink',
                )}
              >
                <Icon className="h-3 w-3" />
                {label}
              </button>
            ))}
        </div>
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Collapse side panel"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconPanel />
        </button>
      </div>

      {/* Tab content: the files panel stays mounted (preserving list/preview state); preview iframes mount lazily */}
      <div className="relative min-h-0 flex-1">
        <div className={cn('h-full', activeTab !== FILES_TAB && 'hidden')}>
          <FilesPanel
            sessionId={sessionId}
            refreshKey={refreshKey}
            openRequest={fileRequest}
            onOpenRequestDone={clearFileRequest}
          />
        </div>
        {preview != null &&
          PREVIEW_TABS.map(({ id, key, label, sandboxAttrs }) =>
            openedPanels.has(id) ? (
              <div key={id} className={cn('h-full', activeTab !== id && 'hidden')}>
                <iframe
                  src={`${previewBase}${preview.panels[key]}`}
                  title={`Sandbox ${label.toLowerCase()}`}
                  className={cn(
                    'h-full w-full bg-bg',
                    dragging && 'pointer-events-none',
                  )}
                  sandbox={sandboxAttrs}
                />
              </div>
            ) : null,
          )}
      </div>
    </div>
  )
}
