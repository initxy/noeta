import {
  ChevronDown,
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { EmptyState } from "../components/EmptyState.jsx";
import { HighlightedCode } from "../components/HighlightedCode.jsx";
import { mergeComposerPref, storedComposerPref } from "../lib/composer-prefs.js";
import { formatBytes, previewView } from "./file-preview.js";
import { previewHitSeq, turnWentIdle } from "./file-refresh.js";

// One tree row. Folders toggle open/closed on click; files select. Indentation
// scales with depth so nesting reads at a glance.
function TreeNode({ node, depth, expanded, onToggle, selected, onSelect }) {
  const pad = 12 + depth * 16;
  if (node.type === "dir") {
    const isOpen = expanded.has(node.path);
    return (
      <li className="file-tree__item">
        <button
          type="button"
          className="file-tree__row file-tree__row--dir"
          style={{ paddingLeft: pad }}
          title={node.path}
          onClick={() => onToggle(node.path)}
        >
          <span className="file-tree__chevron" aria-hidden="true">
            {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </span>
          <span className="file-tree__icon" aria-hidden="true">
            {isOpen ? <FolderOpen size={14} /> : <Folder size={14} />}
          </span>
          <span className="file-tree__name">{node.name}</span>
        </button>
        {isOpen ? (
          <ul className="file-tree__children">
            {node.children.map((child) => (
              <TreeNode
                key={child.path}
                node={child}
                depth={depth + 1}
                expanded={expanded}
                onToggle={onToggle}
                selected={selected}
                onSelect={onSelect}
              />
            ))}
          </ul>
        ) : null}
      </li>
    );
  }
  const isSelected = selected === node.path;
  return (
    <li className="file-tree__item">
      <button
        type="button"
        className={`file-tree__row file-tree__row--file${
          isSelected ? " is-selected" : ""
        }`}
        style={{ paddingLeft: pad }}
        title={node.path}
        onClick={() => onSelect(node.path)}
      >
        <span className="file-tree__chevron" aria-hidden="true" />
        <span className="file-tree__icon" aria-hidden="true">
          <FileIcon size={14} />
        </span>
        <span className="file-tree__name">{node.name}</span>
      </button>
    </li>
  );
}

// The self-contained file-management surface that mounts into the right-dock
// shell (shell stays generic, this component owns all file logic).
// Fetches the active session's tree, holds expand/selection state, and renders
// the tree + a read-only preview of the selected file (issue 02;
// prism syntax highlighting added in issue 03). Re-fetches whenever the active
// task changes or `refreshKey` bumps; the preview re-reads on every selection
// (never cached — a file the agent edits must not serve stale, D5).
//
// issue 04 — two-tier live refresh (the hit-detection
// pure functions live in file-refresh.js):
//   ① Live-refresh the previewed file on change: when `events` (SSE/history event stream) carries a
//      successful write/edit result for the previewed file, re-fetch only that one file (previewHitSeq hit).
//   ② Refresh the tree at turn end: re-fetch the whole tree once on `working` true→false (the turnWentIdle
//      edge), not re-walking on every tool result.
// Plus a manual refresh button in the panel (immediately re-fetches the tree + current preview).
function FilePanel({
  taskId,
  discoverTaskFiles,
  readTaskFile,
  refreshKey,
  events,
  working,
  // clicking an image in the file panel hands its raw URL up to the
  // shared Lightbox to enlarge (the same Lightbox used by chat-bubble thumbnails). Defaults to a no-op; unit
  // tests need not pass it.
  onOpenImage,
}) {
  const [files, setFiles] = useState([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(() => new Set());
  const [selected, setSelected] = useState(null);
  // Whether the file tree is hidden (when hidden, the preview takes the whole panel).
  const [treeHidden, setTreeHidden] = useState(false);
  // U8 second-level splitter: width (px) of the tree column, persisted in the
  // shared composer-prefs blob under `treeWidth`. mergeComposerPref preserves
  // unknown keys, so writing this never disturbs the model / permission / workspace
  // prefs stored alongside it. Clamped to 120px..55% of the panel at drag time.
  const [treeWidth, setTreeWidth] = useState(() => {
    const stored = Number(storedComposerPref().treeWidth);
    return Number.isFinite(stored) && stored >= 120 ? stored : 240;
  });
  // Preview state for the currently-selected file (D5). `previewResult` is the
  // raw readTaskFile body; `previewLoading` gates the "loading" view. Both are
  // cleared on selection change so the pane never flashes the prior file.
  const [previewResult, setPreviewResult] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  // Bumped to force a re-read of the SAME selection (D6 ①: a write/edit landed
  // on the file we're previewing; manual refresh while a file is open). The
  // preview read effect lists it as a dep, so a bump re-runs the fetch.
  const [previewNonce, setPreviewNonce] = useState(0);
  // P7: when the perf gate turns prism OFF for a large file, the user can force
  // it back on for the CURRENTLY-selected file (reset on every selection change).
  const [forceHighlight, setForceHighlight] = useState(false);
  const reqRef = useRef(0);
  const previewReqRef = useRef(0);
  // The highest edit-seq we've already re-read the preview for, per selection.
  // Guards D6 ① against re-firing on unrelated renders / events that touch a
  // different file (a different file was edited ⇒ the hit seq doesn't advance ⇒ the current preview isn't re-pulled in vain).
  const previewHitSeqRef = useRef(-1);
  // Prior `working` value, to detect the running→idle edge for the tree refresh
  // (D6 ②). Seeded false so the very first idle render is not mistaken for an edge.
  const prevWorkingRef = useRef(false);
  // The panel body element (measured for the 55% splitter cap) + the live drag session.
  const bodyRef = useRef(null);
  const dragRef = useRef(null);

  const load = useMemo(
    () =>
      async function load() {
        if (!taskId || !discoverTaskFiles) {
          setFiles([]);
          setTruncated(false);
          return;
        }
        const gen = ++reqRef.current;
        setLoading(true);
        const result = await discoverTaskFiles(taskId);
        if (gen !== reqRef.current) return; // a newer load superseded this one
        setLoading(false);
        // The thin backend's GET /files returns an ALREADY-NESTED tree
        // ({root, tree}) in the exact node shape this panel renders (dirs:
        // {name,path,type:"dir",children}, files: {name,path,type:"file",size}),
        // sorted folders-first — so we render it directly, no client-side
        // flatten→nest step (the old protocol's flat path list is gone).
        if (result && Array.isArray(result.tree)) {
          setFiles(result.tree);
        } else {
          setFiles([]);
        }
        setTruncated(false);
      },
    [taskId, discoverTaskFiles],
  );

  // Re-fetch on session switch / open / explicit refresh; reset per-session UI
  // state (expansion + selection) so a switch never shows the prior tree's
  // open folders. All folders start COLLAPSED — the tree opens only what the
  // user clicks (`expanded` seeded empty, never auto-populated).
  useEffect(() => {
    setExpanded(new Set());
    setSelected(null);
    load();
  }, [load, refreshKey]);

  // `files` already holds the nested, sorted tree from the backend.
  const tree = Array.isArray(files) ? files : [];

  const onToggle = (path) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  // Read the selected file's content on every selection (D5: not cached — the
  // file may have just been edited by the agent). A monotonic gen guards against
  // a slow read for an old selection clobbering the current one. No selection /
  // no fetcher ⇒ clear the preview.
  useEffect(() => {
    if (!selected || !taskId || !readTaskFile) {
      setPreviewResult(null);
      setPreviewLoading(false);
      return;
    }
    const gen = ++previewReqRef.current;
    setPreviewResult(null);
    setPreviewLoading(true);
    (async () => {
      try {
        const result = await readTaskFile(taskId, selected);
        // B5: only the CURRENT generation touches state. A superseded (or failed)
        // read must not clear/keep loading for a request that no longer owns the pane.
        if (gen === previewReqRef.current) setPreviewResult(result);
      } finally {
        // Clearing loading lives in finally so a rejected/superseded read never
        // strands the pane on "Loading…" — but still only for the live generation.
        if (gen === previewReqRef.current) setPreviewLoading(false);
      }
    })();
  }, [selected, taskId, readTaskFile, previewNonce]);

  // Switching selection seeds the per-file "already re-read up to seq" bookmark
  // to the LATEST edit already in the stream — because the `[selected]` read
  // effect above just fetched the current bytes. So only edits arriving AFTER
  // selection (a strictly newer seq) trigger a live re-read; the fresh open
  // doesn't double-fetch. Runs before the live-refresh effect below (declaration
  // order), so that effect sees the seeded bookmark, not a stale -1.
  useEffect(() => {
    previewHitSeqRef.current = previewHitSeq(events, selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  // P7 — reset the manual "force highlight" toggle whenever the selection changes,
  // so forcing prism on for one big file never carries into the next (possibly huge) file.
  useEffect(() => {
    setForceHighlight(false);
  }, [selected]);

  // D6 ① — live-refresh the CURRENTLY-PREVIEWED file. When the event stream
  // carries a successful write/edit whose (normalized) path is the open file,
  // `previewHitSeq` returns that edit's seq; if it's newer than the last one we
  // re-read for, bump the nonce to re-fetch ONLY this one file. Edits to other
  // files don't advance the hit seq, so the preview is never re-pulled in vain.
  useEffect(() => {
    if (!selected) return;
    const hit = previewHitSeq(events, selected);
    if (hit > previewHitSeqRef.current) {
      previewHitSeqRef.current = hit;
      setPreviewNonce((n) => n + 1);
    }
  }, [events, selected]);

  // D6 ② — refresh the TREE once on the running→idle edge (this turn's AI work
  // just finished), so new/deleted files appear. NOT per tool-result (would
  // thrash the tree mid-turn). `turnWentIdle` is the pure edge predicate.
  useEffect(() => {
    const prev = prevWorkingRef.current;
    prevWorkingRef.current = working;
    if (turnWentIdle(prev, working)) load();
  }, [working, load]);

  const preview = previewView({
    selected,
    loading: previewLoading,
    result: previewResult,
    taskId,
  });

  // D6 ③ — manual refresh fallback: re-walk the tree AND re-read the open file
  // (a file the agent edited that didn't auto-refresh, or just a "show me now").
  const manualRefresh = () => {
    load();
    if (selected) setPreviewNonce((n) => n + 1);
  };

  // U8 second-level splitter drag: clamp the tree column to 120px..55% of the
  // panel body, then persist the final width on pointer-up (survives refresh).
  const onSplitterDown = (e) => {
    const body = bodyRef.current;
    if (!body) return;
    e.preventDefault();
    dragRef.current = {
      startX: e.clientX,
      startW: treeWidth,
      panelW: body.getBoundingClientRect().width,
      lastW: treeWidth,
    };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch (_err) {}
  };
  const onSplitterMove = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    const max = Math.max(120, drag.panelW * 0.55);
    const next = Math.min(
      max,
      Math.max(120, drag.startW + (e.clientX - drag.startX)),
    );
    drag.lastW = next;
    setTreeWidth(next);
  };
  const onSplitterUp = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch (_err) {}
    mergeComposerPref({ treeWidth: Math.round(drag.lastW) });
  };

  // P7 — perf-gate telemetry for the notice bar. `gatedOff` = the size gate turned
  // prism OFF for a big file whose language we DO recognize (so "force on" is
  // meaningful); `doHighlight` = actually render prism (gate passed, or user forced).
  const gatedOff =
    preview.mode === "text" && !!preview.language && !preview.highlight;
  const doHighlight =
    preview.mode === "text" &&
    !!preview.language &&
    (preview.highlight || forceHighlight);
  const previewText = preview.mode === "text" ? preview.text : "";
  const previewLines = Number.isFinite(Number(previewResult?.total_lines))
    ? Number(previewResult.total_lines)
    : previewText
      ? previewText.split("\n").length
      : 0;
  const previewBytes = Number.isFinite(Number(previewResult?.size))
    ? Number(previewResult.size)
    : typeof TextEncoder !== "undefined"
      ? new TextEncoder().encode(previewText).length
      : previewText.length;

  return (
    <div className={`file-panel${treeHidden ? " file-panel--tree-hidden" : ""}`}>
      <header className="file-panel__head">
        <span className="file-panel__title">Files</span>
        <button
          type="button"
          className="icon-button file-panel__tree-toggle"
          title={treeHidden ? "Show file tree" : "Hide file tree"}
          aria-label={treeHidden ? "Show file tree" : "Hide file tree"}
          onClick={() => setTreeHidden((v) => !v)}
        >
          {treeHidden ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        </button>
        <button
          type="button"
          className="icon-button file-panel__refresh"
          title="Refresh files"
          aria-label="Refresh files"
          onClick={manualRefresh}
        >
          <RefreshCw size={14} />
        </button>
      </header>
      <div
        className="file-panel__body"
        ref={bodyRef}
        style={{ "--file-tree-w": `${treeWidth}px` }}
      >
        <div className="file-panel__tree" aria-label="Workspace files">
          {tree.length === 0 ? (
            loading ? (
              <p className="file-panel__loading">Loading files…</p>
            ) : (
              <EmptyState kind="files" title="No files" />
            )
          ) : (
            <ul className="file-tree">
              {tree.map((node) => (
                <TreeNode
                  key={node.path}
                  node={node}
                  depth={0}
                  expanded={expanded}
                  onToggle={onToggle}
                  selected={selected}
                  onSelect={setSelected}
                />
              ))}
            </ul>
          )}
          {truncated ? (
            <p className="file-panel__truncated">
              Showing the first 5000 entries.
            </p>
          ) : null}
        </div>
        {/* U8 second-level splitter — drag to resize the tree column (120px..55%). */}
        <div
          className="file-panel__splitter"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize file tree"
          onPointerDown={onSplitterDown}
          onPointerMove={onSplitterMove}
          onPointerUp={onSplitterUp}
        />
        <div className="file-panel__preview">
          {selected ? (
            <div className="file-panel__preview-head">{selected}</div>
          ) : null}
          {/* read-only preview. markdown is shown as raw source
              (never rendered as rich text); text mode gets prism syntax highlighting by extension
              (issue 03), with oversized files falling back to a plain <pre>. The view-model from previewView()
              decides the copy + content + language + highlight gate. */}
          {preview.mode === "empty" ? (
            <EmptyState
              kind="preview"
              title="No file selected"
              hint="Pick a file from the tree to preview it"
            />
          ) : null}
          {preview.mode === "error" ? (
            <EmptyState
              kind="preview"
              title="Can't preview this file"
              hint={preview.notice}
            />
          ) : null}
          {preview.mode === "loading" ? (
            <p className="file-panel__loading">Loading…</p>
          ) : null}
          {preview.mode === "text" ? (
            <>
              {preview.notice ? (
                <p className="file-panel__preview-truncated">{preview.notice}</p>
              ) : null}
              {/* P7 — the perf gate turned prism off for a big file; tell the user
                  why (no silent plain <pre>) and let them force highlighting back on. */}
              {gatedOff && !forceHighlight ? (
                <div className="file-panel__perf-notice">
                  <span>
                    {`Over 1500 lines (${previewLines} lines / ${formatBytes(previewBytes)}); syntax highlighting is off.`}
                  </span>
                  <button
                    type="button"
                    className="file-panel__perf-force"
                    onClick={() => setForceHighlight(true)}
                  >
                    Highlight anyway
                  </button>
                </div>
              ) : null}
              {/* issue 03 — only apply prism highlighting when the language is recognized and the perf gate
                  passes (≤1500 lines and <200KB), or the user forced it (P7); otherwise fall back to a plain
                  <pre> (content still shown in full, just uncolored), to avoid blocking the main thread coloring
                  thousands of lines synchronously. Markdown uses the markdown language to highlight source only. */}
              {doHighlight ? (
                <HighlightedCode
                  code={preview.text}
                  language={preview.language}
                  className="file-panel__preview-pre"
                />
              ) : (
                <pre className="file-panel__preview-pre">{preview.text}</pre>
              )}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export { FilePanel };
