/**
 * File-tree building (flat paths → nested directory tree).
 *
 * The backend returns a flat FileEntry[] ({path, size, mtime}); the frontend
 * builds it into a recursive TreeNode[]. Sort order: directories first,
 * alphabetical within a level (localeCompare, friendly to non-ASCII names).
 * Pure functions, unit-testable.
 */

import type { FileEntry } from '../api/types'

export interface TreeFile {
  type: 'file'
  name: string
  path: string
  size: number
  mtime: number
}

export interface TreeDir {
  type: 'dir'
  name: string
  path: string // virtual directory path with a trailing '/'; used as the toggle key
  children: TreeNode[]
}

export type TreeNode = TreeFile | TreeDir

/** Flat path list → nested tree. The handoff/ prefix is split into partitions by the caller first. */
export function buildTree(entries: FileEntry[]): TreeNode[] {
  const root = new Map<string, TreeNode>()
  // First make sure all implicit directories exist (file a/b/c.md → a/ and a/b/ must be created).
  const ensureDir = (dirPath: string) => {
    if (!dirPath || dirPath === '/') return
    if (root.has(dirPath)) {
      const existing = root.get(dirPath)!
      if (existing.type === 'dir') return
    }
    const slash = dirPath.lastIndexOf('/', dirPath.length - 2)
    const parentPath = slash < 0 ? '' : dirPath.slice(0, slash + 1)
    if (parentPath) ensureDir(parentPath)
    const name = dirPath.slice(parentPath.length, dirPath.length - 1)
    const dir: TreeDir = { type: 'dir', name, path: dirPath, children: [] }
    root.set(dirPath, dir)
    if (parentPath) {
      const parent = root.get(parentPath) as TreeDir | undefined
      parent?.children.push(dir)
    }
  }
  for (const f of entries) {
    // Guard against a leading slash: '/a/b.md' would make the top-level pass
    // misjudge '/a/' (parentPath '/', truthy) as non-root and silently drop the
    // file. The backend does not currently produce such paths; normalize anyway.
    const cleanPath = f.path.replace(/^\/+/, '')
    const slash = cleanPath.lastIndexOf('/')
    const parentPath = slash < 0 ? '' : cleanPath.slice(0, slash + 1)
    const name = slash < 0 ? cleanPath : cleanPath.slice(slash + 1)
    if (!name) continue
    ensureDir(parentPath)
    const node: TreeFile = {
      type: 'file',
      name,
      path: cleanPath,
      size: f.size,
      mtime: f.mtime,
    }
    if (parentPath) {
      const parent = root.get(parentPath) as TreeDir | undefined
      parent?.children.push(node)
    } else {
      root.set(cleanPath, node)
    }
  }

  // Top level (root nodes with parentPath='')
  const top: TreeNode[] = []
  for (const node of root.values()) {
    const slash = node.path.lastIndexOf('/', node.type === 'dir' ? node.path.length - 2 : node.path.length - 1)
    const parentPath = slash < 0 ? '' : node.path.slice(0, slash + 1)
    if (!parentPath) top.push(node)
  }

  const sortTree = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    for (const n of nodes) {
      if (n.type === 'dir') sortTree(n.children)
    }
  }
  sortTree(top)
  return top
}

/** Format a byte count (same as FilesPanel's original formatSize). */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
