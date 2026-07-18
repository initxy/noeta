import { IconFile } from './icons'

interface WorkspaceFileChipProps {
  /** Workspace-relative path */
  path: string
  /** Open the preview in the side panel's "Workspace files" tab */
  onOpen: (path: string) => void
}

/**
 * Workspace-file chip: file-looking text in conversation bodies that matches a
 * workspace file renders as a clickable tag; clicking jumps to the side
 * panel's "Workspace files" tab and selects the preview. inline-flex, so it
 * flows inline with the body text.
 */
export function WorkspaceFileChip({ path, onOpen }: WorkspaceFileChipProps) {
  const name = path.slice(path.lastIndexOf('/') + 1)
  return (
    <button
      type="button"
      onClick={() => onOpen(path)}
      title={`Preview ${path}`}
      className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border bg-surface px-2 py-1 align-middle text-left text-ink transition-colors hover:text-accent"
    >
      <IconFile className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="min-w-0 truncate font-mono text-[11.5px]">{name}</span>
    </button>
  )
}
