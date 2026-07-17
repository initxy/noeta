import { useMemo } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ResolvedKnowledgePath } from '../api/types'
import {
  citeLabelFromHref,
  parseCitationDefs,
  rewriteCitationMarkup,
  type CitationDef,
} from '../lib/citations'
import { buildWorkspaceFileMatcher } from '../lib/workspaceFile'
import { CitationMark } from './Citations'
import { WorkspaceFileChip } from './WorkspaceFileChip'

/**
 * Markdown body. With onOpenFile + workspaceFiles, inline code / relative links
 * that match a workspace file (full path or unique basename, see
 * buildWorkspaceFileMatcher) render as file chips: clicking jumps to the side
 * panel's "Workspace files" preview. With citations, protocol footnotes
 * (`[^n]` + `[^n]: knowledge/...` definition lines) render as citation
 * superscripts + hover excerpt cards (see the lib/citations wire protocol);
 * ordinary footnotes are unaffected — onOpenDoc supplies the origin-link
 * opener for citation chips. External links always open in a new tab (the SPA
 * never navigates away). With none of these, default rendering is used.
 */
export function Markdown({
  text,
  onOpenDoc,
  onOpenFile,
  workspaceFiles,
  citations,
}: {
  text: string
  onOpenDoc?: (url: string) => void
  onOpenFile?: (path: string) => void
  workspaceFiles?: string[]
  /** raw citation path → resolve result (unresolved entries absent; superscripts render pending) */
  citations?: Map<string, ResolvedKnowledgePath>
}) {
  const matchFile = useMemo(
    () =>
      onOpenFile && workspaceFiles?.length
        ? buildWorkspaceFileMatcher(workspaceFiles)
        : null,
    [onOpenFile, workspaceFiles],
  )
  // Protocol footnotes: strip definition lines, rewrite [^n] into #cite-n links
  // (zero cost when citations is not passed).
  const { body, defsByLabel } = useMemo(() => {
    if (!citations) {
      return { body: text, defsByLabel: null as Map<string, CitationDef> | null }
    }
    const defs = parseCitationDefs(text)
    if (defs.length === 0) {
      return { body: text, defsByLabel: null }
    }
    return {
      body: rewriteCitationMarkup(text, defs),
      defsByLabel: new Map(defs.map((d) => [d.label, d])),
    }
  }, [text, citations])
  const components: Components | undefined = useMemo(() => {
    if (!onOpenDoc && !matchFile && !defsByLabel) return undefined
    return {
      a({ href, children, node: _node, ...props }) {
        // Citation superscript: the #cite-n internal link produced by rewriteCitationMarkup
        if (defsByLabel) {
          const label = citeLabelFromHref(href)
          const def = label !== null ? defsByLabel.get(label) : undefined
          if (def) {
            return (
              <CitationMark
                label={def.label}
                resolved={citations?.get(def.raw)}
                onOpenDoc={onOpenDoc}
              />
            )
          }
        }
        // Relative links (the agent often writes [report](report.md)) that hit a
        // workspace file → file chip; the href may be URL-encoded (non-ASCII
        // filenames), so decode before matching.
        if (href && matchFile && onOpenFile) {
          let candidate = href
          try {
            candidate = decodeURIComponent(href)
          } catch {
            // Match malformed encodings as-is
          }
          const path = matchFile(candidate)
          if (path) return <WorkspaceFileChip path={path} onOpen={onOpenFile} />
        }
        return (
          <a href={href} target="_blank" rel="noreferrer" {...props}>
            {children}
          </a>
        )
      },
      code({ children, className, node: _node, ...props }) {
        // Inline code (no newline; block-level code hast text always ends with a
        // newline) that hits a workspace file → chip
        if (matchFile && onOpenFile && typeof children === 'string' && !children.includes('\n')) {
          const path = matchFile(children)
          if (path) return <WorkspaceFileChip path={path} onOpen={onOpenFile} />
        }
        return (
          <code className={className} {...props}>
            {children}
          </code>
        )
      },
    }
  }, [onOpenDoc, onOpenFile, matchFile, defsByLabel, citations])
  return (
    <div className="md-body text-[14.5px]">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {body}
      </ReactMarkdown>
    </div>
  )
}
