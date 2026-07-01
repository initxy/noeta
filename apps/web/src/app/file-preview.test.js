// unit tests for the preview's language-agnostic render decision
// (issue 02 five states + byte formatting). Node built-in runner, zero new deps:
// `node --test src/app/file-preview.test.js`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  HIGHLIGHT_MAX_BYTES,
  HIGHLIGHT_MAX_LINES,
  formatBytes,
  languageForPath,
  previewView,
  shouldHighlight,
} from "./file-preview.js";

test("no file selected → empty mode with the pick-a-file notice", () => {
  const v = previewView({ selected: null, loading: false, result: null });
  assert.equal(v.mode, "empty");
  assert.equal(v.notice, "Select a file to preview");
});

test("selected but still loading → loading mode (no stale text)", () => {
  const v = previewView({ selected: "a.py", loading: true, result: null });
  assert.equal(v.mode, "loading");
});

test("a selected file with no result yet is treated as loading", () => {
  const v = previewView({ selected: "a.py", loading: false, result: null });
  assert.equal(v.mode, "loading");
});

test("plain text → text mode, content passed through, no truncation banner", () => {
  const v = previewView({
    selected: "a.py",
    loading: false,
    result: { kind: "text", content: "print()\n", truncated: false },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.text, "print()\n");
  assert.equal(v.notice, null);
});

test("truncated text → text mode with the 'content truncated' banner", () => {
  const v = previewView({
    selected: "big.txt",
    loading: false,
    // The thin backend's /file caps at a byte budget and flags `truncated`.
    result: { kind: "text", content: "...", truncated: true },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.notice, "Content truncated");
});

test("markdown is text (raw source) — never a special render mode", () => {
  const v = previewView({
    selected: "README.md",
    loading: false,
    result: { kind: "text", content: "# Title\n", truncated: false },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.text, "# Title\n");
});

test("error result → 'file missing or unreadable' notice", () => {
  const v = previewView({
    selected: "gone.txt",
    loading: false,
    result: { kind: "error" },
  });
  assert.equal(v.mode, "error");
  assert.equal(v.notice, "File missing or unreadable");
});

test("formatBytes: sub-10 KB keeps one decimal; ≥10 KB rounds to integer", () => {
  assert.equal(formatBytes(0), "0.1 KB");
  assert.equal(formatBytes(512), "0.5 KB");
  assert.equal(formatBytes(4096), "4 KB");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(20480), "20 KB");
  assert.equal(formatBytes(1024 * 1024), "1024 KB");
});

test("formatBytes: bad input degrades to 0 KB", () => {
  assert.equal(formatBytes(undefined), "0 KB");
  assert.equal(formatBytes(-5), "0 KB");
  assert.equal(formatBytes("x"), "0 KB");
});

// ── issue 03: extension → language mapping ──────────────────────────────────────────────

test("languageForPath: common extensions map to prism's built-in languages", () => {
  assert.equal(languageForPath("a.py"), "python");
  assert.equal(languageForPath("src/app/Foo.jsx"), "jsx");
  assert.equal(languageForPath("x.ts"), "typescript");
  assert.equal(languageForPath("x.tsx"), "tsx");
  assert.equal(languageForPath("main.go"), "go");
  assert.equal(languageForPath("lib.rs"), "rust");
  assert.equal(languageForPath("data.json"), "json");
  assert.equal(languageForPath("conf.yaml"), "yaml");
  assert.equal(languageForPath("conf.yml"), "yaml");
  assert.equal(languageForPath("page.html"), "markup");
  assert.equal(languageForPath("q.sql"), "sql");
});

test("languageForPath: markdown maps to markdown (highlight source, don't render)", () => {
  assert.equal(languageForPath("README.md"), "markdown");
  assert.equal(languageForPath("notes.markdown"), "markdown");
});

test("languageForPath: extension matching is case-insensitive", () => {
  assert.equal(languageForPath("A.PY"), "python");
  assert.equal(languageForPath("X.JSON"), "json");
});

test("languageForPath: unknown extension falls back to null (component treats as plain text, no error)", () => {
  assert.equal(languageForPath("a.xyz"), null);
  assert.equal(languageForPath("weird.qqq"), null);
});

test("languageForPath: no extension / dotfile / empty input falls back to null", () => {
  assert.equal(languageForPath("Makefile"), null);
  assert.equal(languageForPath("Dockerfile"), null);
  assert.equal(languageForPath(".gitignore"), null);
  assert.equal(languageForPath("dir/.env"), null);
  assert.equal(languageForPath(""), null);
  assert.equal(languageForPath(null), null);
  assert.equal(languageForPath(undefined), null);
});

// ── issue 03: performance gate shouldHighlight ──────────────────────────────────────

test("shouldHighlight: small file (lines + bytes both under threshold) → highlight", () => {
  assert.equal(shouldHighlight({ total_lines: 100, size: 5 * 1024 }), true);
  assert.equal(shouldHighlight({ total_lines: 1500, size: 200 * 1024 - 1 }), true);
});

test("shouldHighlight: over 1500 lines → fall back to plain text", () => {
  assert.equal(shouldHighlight({ total_lines: 1501, size: 1024 }), false);
  assert.equal(shouldHighlight({ total_lines: 9000, size: 1024 }), false);
});

test("shouldHighlight: ≥200KB → fall back to plain text", () => {
  assert.equal(shouldHighlight({ total_lines: 10, size: HIGHLIGHT_MAX_BYTES }), false);
  assert.equal(shouldHighlight({ total_lines: 10, size: 500 * 1024 }), false);
});

test("shouldHighlight: boundary — exactly 1500 lines / exactly 200KB-1 bytes still highlights", () => {
  assert.equal(shouldHighlight({ total_lines: HIGHLIGHT_MAX_LINES, size: 0 }), true);
  assert.equal(
    shouldHighlight({ total_lines: 0, size: HIGHLIGHT_MAX_BYTES - 1 }),
    true,
  );
});

test("shouldHighlight: missing metadata → estimate line count from content", () => {
  // 5 short lines → highlight
  assert.equal(shouldHighlight({ content: "a\nb\nc\nd\ne" }), true);
  // content with >1500 lines → fall back
  const huge = Array.from({ length: 1600 }, () => "x").join("\n");
  assert.equal(shouldHighlight({ content: huge }), false);
});

test("shouldHighlight: missing metadata → estimate from content's UTF-8 byte count", () => {
  // single line but >200KB → fall back (even though line count is under)
  const bigLine = "x".repeat(HIGHLIGHT_MAX_BYTES + 10);
  assert.equal(shouldHighlight({ content: bigLine }), false);
});

test("shouldHighlight: no info at all → highlight conservatively (most files are small)", () => {
  assert.equal(shouldHighlight({}), true);
  assert.equal(shouldHighlight(null), true);
  assert.equal(shouldHighlight(undefined), true);
});

// ── issue 03: previewView text state carries language + highlight ──────────────────

test("previewView(text): small code file carries language + highlight=true", () => {
  const v = previewView({
    selected: "src/main.py",
    loading: false,
    result: { kind: "text", content: "print()\n", total_lines: 1, size: 8 },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.language, "python");
  assert.equal(v.highlight, true);
});

test("previewView(text): over-threshold file → highlight=false (falls back to plain <pre>)", () => {
  const v = previewView({
    selected: "big.py",
    loading: false,
    result: { kind: "text", content: "...", total_lines: 5000, size: 1024 },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.highlight, false);
});

test("previewView(text): unknown extension → language=null (component falls back to plain <pre>)", () => {
  const v = previewView({
    selected: "data.xyz",
    loading: false,
    result: { kind: "text", content: "blob", total_lines: 1, size: 4 },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.language, null);
  // highlight can still be true (small file), but the component gates on
  // `highlight && language`, so language=null ⇒ plain <pre>.
});

test("previewView(text): markdown carries the markdown language (highlight source, still text state)", () => {
  const v = previewView({
    selected: "README.md",
    loading: false,
    result: { kind: "text", content: "# Title\n", total_lines: 1, size: 8 },
  });
  assert.equal(v.mode, "text");
  assert.equal(v.language, "markdown");
  assert.equal(v.highlight, true);
});
