import { describe, expect, it } from 'vitest'
import { buildTree, formatSize } from './fileTree'
import type { FileEntry } from '../api/types'

function e(path: string, size = 100): FileEntry {
  return { path, size, mtime: 0 }
}

describe('buildTree', () => {
  it('empty list → empty tree', () => {
    expect(buildTree([])).toEqual([])
  })

  it('root-level files stay flat', () => {
    const tree = buildTree([e('a.md'), e('b.txt')])
    expect(tree).toHaveLength(2)
    expect(tree.map((n) => n.name)).toEqual(['a.md', 'b.txt'])
    expect(tree[0].type).toBe('file')
  })

  it('nested files auto-create parent directories; directories first, then alphabetical', () => {
    const tree = buildTree([
      e('src/lib/x.ts'),
      e('src/app.ts'),
      e('README.md'),
      e('package.json'),
    ])
    // Top level: src (dir) before files; files in localeCompare order (lowercase before uppercase).
    expect(tree.map((n) => n.name)).toEqual(['src', 'package.json', 'README.md'])
    const src = tree[0]
    expect(src.type).toBe('dir')
    if (src.type === 'dir') {
      // Inside src: lib before app.ts (dir first); lib contains x.ts.
      expect(src.children.map((c) => c.name)).toEqual(['lib', 'app.ts'])
      const lib = src.children[0]
      expect(lib.type).toBe('dir')
      if (lib.type === 'dir') {
        expect(lib.children.map((c) => c.name)).toEqual(['x.ts'])
      }
    }
  })

  it('implicit intermediate directories are created (even when not listed)', () => {
    const tree = buildTree([e('a/b/c/d.txt')])
    expect(tree).toHaveLength(1)
    const a = tree[0]
    expect(a.type).toBe('dir')
    expect(a.name).toBe('a')
  })

  it('handoff flat case (the handoff directory holds single-level files)', () => {
    const tree = buildTree([
      e('handoff/requirements.md'),
      e('handoff/node-a.md'),
    ])
    // handoff is filtered out before being passed in; this just shows the pre-filter tree shape.
    expect(tree).toHaveLength(1)
    expect(tree[0].type).toBe('dir')
    if (tree[0].type === 'dir') {
      expect(tree[0].children.map((c) => c.name)).toEqual([
        'node-a.md',
        'requirements.md',
      ])
    }
  })

  it('normalizes leading slashes (defends against odd backend paths; never drops files silently)', () => {
    const tree = buildTree([e('/a/b.md'), e('/c.md')])
    expect(tree.map((n) => n.name)).toEqual(['a', 'c.md'])
    const a = tree[0]
    expect(a.type).toBe('dir')
    if (a.type === 'dir') {
      expect(a.children.map((c) => c.path)).toEqual(['a/b.md'])
    }
    expect(tree[1].path).toBe('c.md')
  })

  it('non-ASCII names sort stably via localeCompare', () => {
    const tree = buildTree([e('éclair.md'), e('README.md'), e('a/analysis.md')])
    expect(tree.map((n) => n.name)).toEqual(['a', 'éclair.md', 'README.md'])
  })
})

describe('formatSize', () => {
  it('B / KB / MB', () => {
    expect(formatSize(100)).toBe('100 B')
    expect(formatSize(2048)).toBe('2.0 KB')
    expect(formatSize(2 * 1024 * 1024)).toBe('2.0 MB')
  })
})
