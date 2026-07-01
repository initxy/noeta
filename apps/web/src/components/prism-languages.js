// Prism language gating + size gate for the shared code highlighter (B18).
//
// Pure, framework-free (no JSX) so it is unit-testable under `node --test`. The
// hard rule: NEVER hand `<Highlight>` a language whose grammar is not actually
// bundled in this `prism-react-renderer` build — it would silently fall back to
// plain tokens (a false "highlighted" state). Both the markdown fence path and
// the file-preview extension path resolve through `isPrismLanguageSupported`, so
// an unmapped / unbundled language cleanly renders as plain text instead.

import { Prism } from "prism-react-renderer";

// True only when `lang` is a grammar actually present in this build's
// `Prism.languages` (aliases like js / html / md count; the util functions
// `extend` / `insertBefore` are functions, not grammars, so they're excluded).
function isPrismLanguageSupported(lang) {
  return Boolean(lang) && typeof Prism.languages[lang] === "object";
}

// Map a markdown fence info-string ("```js") onto a prism language NAME. The map
// is a convenience superset (short aliases → canonical names); the support gate
// below is the real filter, so an entry whose grammar isn't bundled (bash, ruby,
// diff, …) still returns null and falls back to plain.
const FENCE_TO_PRISM = {
  js: "javascript",
  javascript: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  node: "javascript",
  jsx: "jsx",
  ts: "typescript",
  typescript: "typescript",
  tsx: "tsx",
  py: "python",
  python: "python",
  go: "go",
  golang: "go",
  rs: "rust",
  rust: "rust",
  kt: "kotlin",
  kotlin: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  "c++": "cpp",
  hpp: "cpp",
  objc: "objectivec",
  objectivec: "objectivec",
  json: "json",
  jsonc: "json",
  json5: "json",
  yaml: "yaml",
  yml: "yaml",
  css: "css",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  vue: "markup",
  markup: "markup",
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
  md: "markdown",
  markdown: "markdown",
};

// Resolve a fence hint to a SUPPORTED prism language, or null (→ plain text).
function prismLanguage(fence) {
  if (!fence) return null;
  const key = String(fence).trim().toLowerCase().split(/\s+/)[0];
  const name = FENCE_TO_PRISM[key] || null;
  return name && isPrismLanguageSupported(name) ? name : null;
}

// Perf gate (mirrors file-preview's shouldHighlight): prism builds the token DOM
// synchronously, so skip very large blocks. Size is measured in UTF-8 BYTES, not
// characters — a block of CJK / emoji is far larger than its `.length`.
const MAX_HIGHLIGHT_BYTES = 200 * 1024;
const MAX_HIGHLIGHT_LINES = 1500;

function utf8ByteLength(code) {
  return typeof TextEncoder !== "undefined"
    ? new TextEncoder().encode(code).length
    : code.length;
}

function canHighlightSize(code) {
  if (typeof code !== "string") return false;
  // UTF-8 is ≥1 byte/char, so a char length already at/over the cap is over the
  // byte cap too — cheap early return that avoids allocating a TextEncoder buffer
  // for a huge ASCII block. `>=` matches file-preview's shouldHighlight().
  if (code.length >= MAX_HIGHLIGHT_BYTES) return false;
  if (utf8ByteLength(code) >= MAX_HIGHLIGHT_BYTES) return false;
  let lines = 1;
  for (let i = 0; i < code.length; i += 1) {
    if (code[i] === "\n") lines += 1;
    if (lines > MAX_HIGHLIGHT_LINES) return false;
  }
  return true;
}

export {
  isPrismLanguageSupported,
  prismLanguage,
  canHighlightSize,
  utf8ByteLength,
  FENCE_TO_PRISM,
};
