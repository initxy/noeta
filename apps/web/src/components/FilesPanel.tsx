import { useCallback, useEffect, useRef, useState } from 'react'
import { sessionsApi } from '../api/endpoints'
import type { FileContent, FileEntry } from '../api/types'
import { cn } from '../lib/cn'
import { buildTree, formatSize, type TreeNode } from '../lib/fileTree'
import {
  IconChevron,
  IconFile,
  IconFolder,
  IconFolderOpen,
  IconRefresh,
  IconSidebar,
} from './icons'
import { Markdown } from './Markdown'

/** Open request initiated externally (a file chip in the conversation flow): nonce increments so the same file can be clicked repeatedly. */
export interface FileOpenRequest {
  path: string
  nonce: number
}

const TREE_WIDTH_KEY = 'noeta-files-tree-width'
const TREE_COLLAPSED_KEY = 'noeta-files-tree-collapsed'
const TREE_MIN_WIDTH = 160
const TREE_DEFAULT_WIDTH = 240
/** Minimum width guaranteed to the preview area while dragging the tree column wider. */
const PREVIEW_MIN_WIDTH = 240

interface FilesPanelProps {
  sessionId: string
  /** Triggers a refresh on change (turn_finished counter) */
  refreshKey: number
  /** Open request from clicking a file chip in the conversation flow (produced by useSidePanel.openWorkspaceFile) */
  openRequest?: FileOpenRequest | null
  /** Request consumed: notify the holder to clear it. The component unmounts when
   * the panel collapses, so the request must survive until consumed after remount */
  onOpenRequestDone?: () => void
}

/**
 * Tree-node row (recursive): directory rows toggle expand/collapse on click,
 * file rows open the preview. Indent grows with depth (8 + depth*14, tightened
 * for the narrow side panel). Directories default to expanded (agent output is
 * usually shallow with few files, so new artifacts are visible immediately);
 * paths the user collapsed live in the collapsed set and survive refreshes.
 */
function TreeRow({
  node,
  depth,
  collapsed,
  onToggle,
  selected,
  onSelect,
}: {
  node: TreeNode
  depth: number
  collapsed: Set<string>
  onToggle: (path: string) => void
  selected: string | null
  onSelect: (path: string) => void
}) {
  const pad = 8 + depth * 14
  if (node.type === 'dir') {
    const isOpen = !collapsed.has(node.path)
    return (
      <li>
        <button
          type="button"
          onClick={() => onToggle(node.path)}
          title={node.path}
          className="flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left transition-colors hover:bg-surface-2"
          style={{ paddingLeft: pad }}
        >
          <IconChevron open={isOpen} className="h-3 w-3 shrink-0 text-ink-3" />
          {isOpen ? (
            <IconFolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-3" />
          ) : (
            <IconFolder className="h-3.5 w-3.5 shrink-0 text-ink-3" />
          )}
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink">
            {node.name}
          </span>
        </button>
        {isOpen && (
          <ul>
            {node.children.map((child) => (
              <TreeRow
                key={child.path}
                node={child}
                depth={depth + 1}
                collapsed={collapsed}
                onToggle={onToggle}
                selected={selected}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </li>
    )
  }
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(node.path)}
        title={node.path}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left transition-colors',
          selected === node.path ? 'bg-accent-soft' : 'hover:bg-surface-2',
        )}
        style={{ paddingLeft: pad }}
      >
        {/* Placeholder as wide as the directory-row chevron so icons/names align vertically */}
        <span className="h-3 w-3 shrink-0" aria-hidden />
        <IconFile className="h-3.5 w-3.5 shrink-0 text-ink-3" />
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink">
          {node.name}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-ink-3">
          {formatSize(node.size)}
        </span>
      </button>
    </li>
  )
}

/**
 * "Workspace files" tab content: directory tree + content preview (markdown
 * rendered), filling the side panel. When a preview is open the tree is a
 * fixed-pixel narrow column: its right edge is draggable and the whole column
 * can collapse; width / collapsed state persist (the same pattern as
 * SidePanel's width persistence).
 */
export function FilesPanel({
  sessionId,
  refreshKey,
  openRequest,
  onOpenRequestDone,
}: FilesPanelProps) {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<FileContent | null>(null)
  const [contentLoading, setContentLoading] = useState(false)
  // Directory paths the user explicitly collapsed. Directories default to
  // expanded, so we track a "collapsed set" rather than an "expanded set":
  // refreshes (refreshKey) keep the user's collapses while newly appearing
  // directories stay visible by default.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [treeWidth, setTreeWidth] = useState(() => {
    const saved = Number(localStorage.getItem(TREE_WIDTH_KEY))
    return Number.isFinite(saved) && saved >= TREE_MIN_WIDTH
      ? saved
      : TREE_DEFAULT_WIDTH
  })
  const [treeCollapsed, setTreeCollapsed] = useState(
    () => localStorage.getItem(TREE_COLLAPSED_KEY) === '1',
  )
  const [dragging, setDragging] = useState(false)
  // View mode for md / html files (rendered preview vs source); kept across
  // files (no repeated toggling when reading several files in a row)
  const [richView, setRichView] = useState<'preview' | 'source'>('preview')
  const splitRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    localStorage.setItem(TREE_WIDTH_KEY, String(treeWidth))
  }, [treeWidth])
  useEffect(() => {
    localStorage.setItem(TREE_COLLAPSED_KEY, treeCollapsed ? '1' : '0')
  }, [treeCollapsed])

  // Drag listeners live on window, driven by an effect: both pointerup and unmount clean up
  useEffect(() => {
    if (!dragging) return
    const onMove = (ev: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect()
      if (!rect) return
      const max = Math.max(TREE_MIN_WIDTH, rect.width - PREVIEW_MIN_WIDTH)
      setTreeWidth(Math.min(Math.max(ev.clientX - rect.left, TREE_MIN_WIDTH), max))
    }
    const onUp = () => setDragging(false)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [dragging])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await sessionsApi.files(sessionId)
      setFiles(r.files)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    setSelected(null)
    setContent(null)
    void load()
  }, [load, refreshKey])

  // Session switch: collapse state is remembered by path, meaningless across sessions — reset
  useEffect(() => {
    setCollapsed(new Set())
  }, [sessionId])

  const toggleDir = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const openFile = async (path: string) => {
    setSelected(path)
    setContentLoading(true)
    setContent(null)
    try {
      setContent(await sessionsApi.fileContent(sessionId, path))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to read the file')
    } finally {
      setContentLoading(false)
    }
  }

  // Open request from a conversation file chip: consume once, then call back to
  // clear (clearing lives with the holder — this component unmounts when the
  // panel collapses, so the request must survive until consumed after remount).
  // If ancestor directories were manually collapsed, expand them first so the
  // selected row is visible.
  useEffect(() => {
    if (!openRequest) return
    setCollapsed((prev) => {
      const next = new Set(prev)
      let idx = openRequest.path.lastIndexOf('/')
      while (idx >= 0) {
        next.delete(openRequest.path.slice(0, idx + 1))
        idx = openRequest.path.lastIndexOf('/', idx - 1)
      }
      return next
    })
    void openFile(openRequest.path)
    onOpenRequestDone?.()
    // openFile / onOpenRequestDone are unstable references; the request object is
    // consumed once and only its change triggers this effect
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest])

  // Workflow sessions show handoff/ trace files and business documents in
  // separate partitions; without handoff/ files this degrades to a single-tree
  // view with unchanged behavior.
  const visible = files
  const handoffFiles = visible.filter((f) => f.path.startsWith('handoff/'))
  const docFiles = visible.filter((f) => !f.path.startsWith('handoff/'))
  const docTree = buildTree(docFiles)
  // The handoff partition builds its tree from full paths, so the top level is
  // a single handoff/ directory — render its children; the partition heading is
  // the directory name (usually flat, degrading to file rows; preview/selection
  // naturally keeps full paths)
  const handoffRoot = buildTree(handoffFiles)[0]
  const handoffTree =
    handoffRoot && handoffRoot.type === 'dir' ? handoffRoot.children : []

  const renderTree = (nodes: TreeNode[]) => (
    <ul>
      {nodes.map((n) => (
        <TreeRow
          key={n.path}
          node={n}
          depth={0}
          collapsed={collapsed}
          onToggle={toggleDir}
          selected={selected}
          onSelect={(p) => void openFile(p)}
        />
      ))}
    </ul>
  )

  // md and html both have "rendered preview / source" modes; other files are plain text only
  const isMd = selected?.endsWith('.md') ?? false
  const isHtml = selected?.endsWith('.html') ?? false
  const renderable = isMd || isHtml

  return (
    <div className="flex h-full w-full flex-col bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-[11px] text-ink-3">
          {loading
            ? 'Loading…'
            : `${visible.length} file${visible.length === 1 ? '' : 's'}`}
        </span>
        <button
          type="button"
          onClick={() => void load()}
          title="Refresh file list"
          className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <IconRefresh className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Tree left, preview right: with a file selected the tree is a draggable /
          collapsible narrow column and the preview fills the rest; without a
          selection the tree takes the full width */}
      <div ref={splitRef} className="flex min-h-0 flex-1">
        <div
          style={selected && !treeCollapsed ? { width: treeWidth } : undefined}
          className={cn(
            'overflow-y-auto',
            selected && treeCollapsed && 'hidden',
            selected && !treeCollapsed && 'max-w-[70%] shrink-0 border-r border-border',
            !selected && 'flex-1',
          )}
        >
          {loading ? (
            <div className="space-y-2 p-3">
              {[0, 1].map((i) => (
                <div key={i} className="h-8 animate-pulse rounded-md bg-surface-2" />
              ))}
            </div>
          ) : error ? (
            <p className="p-4 text-[12.5px] leading-relaxed text-danger">{error}</p>
          ) : visible.length === 0 ? (
            <p className="p-4 text-center text-[12.5px] leading-relaxed text-ink-3">
              Files the agent produces will appear here.
            </p>
          ) : handoffFiles.length > 0 ? (
            // Partitions: business documents + handoff, one tree each
            <div className="p-1.5">
              {docFiles.length > 0 && (
                <>
                  <p className="px-2 pb-1 pt-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-3">
                    Deliverables
                  </p>
                  {renderTree(docTree)}
                </>
              )}
              <p className="px-2 pb-1 pt-2.5 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-3">
                Handoff
              </p>
              {renderTree(handoffTree)}
            </div>
          ) : (
            <div className="p-1.5">{renderTree(docTree)}</div>
          )}
        </div>

        {selected && (
          <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
            {/* Drag handle between tree and preview (nothing to drag while the tree is collapsed) */}
            {!treeCollapsed && (
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
            )}

            {/* Preview toolbar: collapse/expand tree + file path + md/html source/preview toggle */}
            <div className="flex h-9 shrink-0 items-center gap-1.5 border-b border-border pl-1.5 pr-2">
              <button
                type="button"
                onClick={() => setTreeCollapsed((v) => !v)}
                title={treeCollapsed ? 'Expand file tree' : 'Collapse file tree'}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
              >
                <IconSidebar className="h-3.5 w-3.5" />
              </button>
              <span
                className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-2"
                title={selected}
              >
                {selected}
              </span>
              {renderable && (
                <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-border p-0.5">
                  {(
                    [
                      { key: 'preview', label: 'Preview' },
                      { key: 'source', label: 'Source' },
                    ] as const
                  ).map(({ key, label }) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setRichView(key)}
                      className={cn(
                        'rounded-md px-2 py-0.5 text-[11px] transition-colors',
                        richView === key
                          ? 'bg-accent-soft font-medium text-ink'
                          : 'text-ink-3 hover:text-ink',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Preview content: md → Markdown, html → sandboxed iframe (srcDoc lands
               on a null origin: its scripts cannot read the parent page's
               cookies/API/localStorage), everything else → plain text; the html
               preview fills the area with no padding — the page controls its own layout. */}
            {contentLoading ? (
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3.5">
                {[0, 1, 2].map((i) => (
                  <div key={i} className="h-4 animate-pulse rounded bg-surface-2" />
                ))}
              </div>
            ) : !content ? (
              <div className="min-h-0 flex-1" />
            ) : isHtml && richView === 'preview' ? (
              <div className="flex min-h-0 flex-1 flex-col">
                {content.truncated && (
                  <p className="shrink-0 border-b border-border bg-surface-2 px-2 py-1 font-mono text-[10.5px] text-warn">
                    File too large; only the beginning is rendered (may display incompletely)
                  </p>
                )}
                <iframe
                  title={selected}
                  srcDoc={content.content}
                  sandbox="allow-scripts"
                  className={cn(
                    'min-h-0 w-full flex-1 border-0 bg-white',
                    dragging && 'pointer-events-none',
                  )}
                />
              </div>
            ) : (
              <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
                {content.truncated && (
                  <p className="mb-2 rounded-md bg-surface-2 px-2 py-1 font-mono text-[10.5px] text-warn">
                    File too large; showing only the beginning
                  </p>
                )}
                {isMd && richView === 'preview' ? (
                  <Markdown text={content.content} />
                ) : (
                  <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-ink-2">
                    {content.content}
                  </pre>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
