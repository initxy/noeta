// Regression coverage for the "blank dock" fix: `panelType` is
// session-persistent and can name a preview-only tab (browser/terminal/code)
// left over from a session that HAD a sandbox. If the active session has none
// (or the container deallocates mid-session), every pane used to render
// `hidden` with no active tab — a blank dock until the user manually clicked
// Files.
//
// RightDock.jsx is a JSX module (pulls in react + lucide-react), so — same
// approach as fix-trace.test.js / trace-improvements.test.js — the pure
// `resolveEffectivePanelType` helper is re-implemented inline here to pin its
// behavioral contract, and the source text is asserted against to confirm
// RightDock.jsx actually renders off that derivation instead of the raw
// `panelType` (which node --test can't parse/import as a JSX module).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const src = readFileSync(
  fileURLToPath(new URL("./RightDock.jsx", import.meta.url)),
  "utf8",
);

// --- structural wiring -------------------------------------------------

test("resolveEffectivePanelType is defined and exported for reuse/testing", () => {
  assert.match(
    src,
    /function resolveEffectivePanelType\(panelType, hasPreview\)/,
    "resolveEffectivePanelType helper is missing or its signature changed",
  );
  assert.match(
    src,
    /export \{ RightDock, resolveEffectivePanelType \};/,
    "resolveEffectivePanelType should stay exported",
  );
});

test("tabs and panes render off effectivePanelType, not the raw panelType", () => {
  assert.match(
    src,
    /const effectivePanelType = resolveEffectivePanelType\(panelType, hasPreview\);/,
    "effectivePanelType must be derived via resolveEffectivePanelType",
  );
  // The always-available Files/App tab highlight + pane visibility must key
  // off the derived value — these are exactly the panes that stayed
  // unconditional while a preview-only tab could blank out the dock.
  assert.match(src, /aria-selected=\{effectivePanelType === "files"\}/);
  assert.match(src, /aria-selected=\{effectivePanelType === "app"\}/);
  assert.match(src, /hidden=\{effectivePanelType !== "files"\}/);
  assert.match(src, /hidden=\{effectivePanelType !== "app"\}/);
});

// --- behavioral contract (mirrored inline) ------------------------------
// Mirrors RightDock.jsx's resolveEffectivePanelType exactly, so the contract
// is pinned without importing the JSX module.
const PREVIEW_ONLY_PANEL_TYPES = new Set(["browser", "terminal", "code"]);
function resolveEffectivePanelType(panelType, hasPreview) {
  if (!hasPreview && PREVIEW_ONLY_PANEL_TYPES.has(panelType)) return "files";
  return panelType;
}

test("a preview-only panelType with no live preview falls back to files", () => {
  assert.equal(resolveEffectivePanelType("browser", false), "files");
  assert.equal(resolveEffectivePanelType("terminal", false), "files");
  assert.equal(resolveEffectivePanelType("code", false), "files");
});

test("a preview-only panelType with a live preview renders as-is", () => {
  assert.equal(resolveEffectivePanelType("browser", true), "browser");
  assert.equal(resolveEffectivePanelType("terminal", true), "terminal");
  assert.equal(resolveEffectivePanelType("code", true), "code");
});

test("always-available tabs (files/app) are never overridden, preview or not", () => {
  assert.equal(resolveEffectivePanelType("files", false), "files");
  assert.equal(resolveEffectivePanelType("app", false), "app");
  assert.equal(resolveEffectivePanelType("files", true), "files");
  assert.equal(resolveEffectivePanelType("app", true), "app");
});

test("the stored choice is not clobbered — it resurfaces once hasPreview returns", () => {
  // Simulates: user is on "terminal", the sandbox deallocates (hasPreview
  // flips false, falls back to files), then a new container comes up
  // (hasPreview flips true again) — the ORIGINAL panelType still resolves to
  // "terminal" because it was never mutated, only the render derivation.
  const storedPanelType = "terminal";
  assert.equal(resolveEffectivePanelType(storedPanelType, false), "files");
  assert.equal(resolveEffectivePanelType(storedPanelType, true), "terminal");
});
