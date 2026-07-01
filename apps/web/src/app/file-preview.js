// The file preview's language-agnostic render decision, pulled out
// of FilePanel.jsx so it is unit-testable apart from React (see
// file-preview.test.js). Pure: maps the {selected, loading, result} inputs to a
// flat view-model the component renders without any further branching.
//
// `result` is whatever `readTaskFile` returned — one of:
//   {kind:"text", content, truncated, size, ...}
//   {kind:"error"}            (non-200 / network fault — file deleted / unreadable)
//
// The thin backend's GET /file serves a single sandboxed workspace and decodes
// every file as UTF-8 text (the old protocol's binary/image preview kinds are
// gone, runtime/sdk/app three-layer refactor T7), so the preview is text-or-error only.
// The component never decides the wording itself; it reads `mode` + `text` + `notice`.

import { isPrismLanguageSupported } from "../components/prism-languages.js";

// Render bytes as a compact "X KB" (1 decimal under 10 KB, else integer). Sub-KB
// rounds up to a floor of 0.1 KB so a tiny binary still reads as a size, not "0".
function formatBytes(size) {
  const n = Number(size);
  if (!Number.isFinite(n) || n < 0) return "0 KB";
  const kb = n / 1024;
  if (kb < 10) return `${Math.max(0.1, Math.round(kb * 10) / 10)} KB`;
  return `${Math.round(kb)} KB`;
}

// syntax-highlight performance gate. prism colorizes
// synchronously, so a few thousand lines block the main thread. Only highlight "small files": ≤1500 lines
// AND <200KB (whichever hits first). Larger files fall back to plain <pre> — content still shown in full,
// just uncolored. A pure function so it's unit-testable, not buried in JSX.
//
// `meta` comes from readTaskFile's body (text state): { total_lines, size, content }.
// When metadata is missing, fall back to content (lines from newline count, bytes from UTF-8 encoded
// length). If still undeterminable, highlight conservatively (most files are small, and the estimate
// usually suffices).
const HIGHLIGHT_MAX_LINES = 1500;
const HIGHLIGHT_MAX_BYTES = 200 * 1024;

function shouldHighlight(meta) {
  const m = meta || {};
  let lines = Number(m.total_lines);
  if (!Number.isFinite(lines) && typeof m.content === "string") {
    // A missing trailing newline still counts as one line; empty string is 0 lines.
    lines = m.content.length === 0 ? 0 : m.content.split("\n").length;
  }
  if (Number.isFinite(lines) && lines > HIGHLIGHT_MAX_LINES) return false;

  let bytes = Number(m.size);
  if (!Number.isFinite(bytes) && typeof m.content === "string") {
    // No server-side size — estimate from UTF-8 byte count.
    bytes =
      typeof TextEncoder !== "undefined"
        ? new TextEncoder().encode(m.content).length
        : m.content.length;
  }
  if (Number.isFinite(bytes) && bytes >= HIGHLIGHT_MAX_BYTES) return false;

  return true;
}

// pick the prism language by file extension. Only maps to
// prism-react-renderer's built-in language set (js/ts/py/go/rust/json/yaml/css/html/sql/md/c/cpp/swift/kotlin…);
// no extra language imports that bloat the bundle. Unknown extensions return null (component treats them as
// no-language/plain-text; prism still renders, just uncolored, never errors). Markdown still only highlights
// source, never typesets it (D5).
const EXT_TO_LANG = {
  js: "javascript",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",
  mts: "typescript",
  cts: "typescript",
  py: "python",
  pyi: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  kts: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  hh: "cpp",
  m: "objectivec",
  mm: "objectivec",
  cs: "csharp",
  php: "php",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  json: "json",
  jsonc: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  css: "css",
  scss: "scss",
  sass: "sass",
  less: "less",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  vue: "markup",
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
};

function languageForPath(path) {
  const p = String(path || "");
  // Take the extension after the last dot; no extension (e.g. Makefile, Dockerfile) falls back to null.
  const slash = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  const base = p.slice(slash + 1);
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return null; // no extension, or dotfile like ".gitignore" → plain-text fallback
  const ext = base.slice(dot + 1).toLowerCase();
  const lang = EXT_TO_LANG[ext] ?? null;
  // Only return a language whose grammar is ACTUALLY bundled in this prism build
  // (e.g. .rb / .php / .sh map to names with no grammar here → plain fallback);
  // handing an unbundled grammar to <Highlight> silently degrades to plain.
  return lang && isPrismLanguageSupported(lang) ? lang : null;
}

// Decide what the preview pane shows. Returns:
//   { mode, text?, notice?, language?, highlight? }
// mode ∈ "empty" | "loading" | "text" | "error".
//   - "empty":   no file selected            → "Select a file to preview" (notice)
//   - "loading": a read is in flight
//   - "text":    plain <pre> source (md included — raw, never rendered); `text`
//                is the content, `notice` carries the truncation banner or null,
//                `language` is the prism language picked by extension (null if unknown),
//                `highlight` is the performance-gate verdict (false ⇒ component falls back to plain <pre>).
//   - "error":   "File missing or unreadable"       (notice)
function previewView({ selected, loading, result }) {
  if (!selected) return { mode: "empty", notice: "Select a file to preview" };
  if (loading || !result) return { mode: "loading" };
  if (result.kind === "text") {
    const truncated = !!result.truncated;
    const notice = truncated ? "Content truncated" : null;
    const text = result.content ?? "";
    return {
      mode: "text",
      text,
      notice,
      language: languageForPath(selected),
      highlight: shouldHighlight({
        total_lines: result.total_lines,
        size: result.size,
        content: text,
      }),
    };
  }
  // kind === "error" or anything unexpected → the read-failure state.
  return { mode: "error", notice: "File missing or unreadable" };
}

export {
  formatBytes,
  previewView,
  shouldHighlight,
  languageForPath,
  HIGHLIGHT_MAX_LINES,
  HIGHLIGHT_MAX_BYTES,
};
