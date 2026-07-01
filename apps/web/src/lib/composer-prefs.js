// Last-used composer selections (model / permission / provider / workspace),
// persisted to localStorage so a NEW session opens with the choices
// you made last time instead of the bare defaults — no re-picking every time. Mirrors the
// resilience of lib/theme.js: every access is wrapped so a disabled / full
// localStorage degrades to "no remembered prefs", never an exception.
//
// These are the *new-session* defaults only. When you switch INTO an existing
// session the composer is hydrated from that session's own bound config (see
// ChatApp's session-switch effect), which deliberately does NOT write here — so
// re-opening an old session never clobbers your new-session defaults.

const COMPOSER_PREFS_KEY = "noeta-composer-prefs";

function storedComposerPref() {
  try {
    const raw = window.localStorage.getItem(COMPOSER_PREFS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function mergeComposerPref(patch) {
  try {
    const next = { ...storedComposerPref(), ...patch };
    window.localStorage.setItem(COMPOSER_PREFS_KEY, JSON.stringify(next));
  } catch (error) {}
}

// the right-dock file panel's open state + width, persisted so the
// panel opens at the size you left it and (per the open flag) stays put across a
// refresh. Same resilience contract as the composer prefs above: every access is
// wrapped so a disabled / full localStorage degrades to the defaults below.
const PANEL_PREFS_KEY = "noeta-panel-prefs";
// The panel is now "tree on the left + preview on the right" side by side, so it needs a wider
// drag range (MIN must fit both columns). MAX is just a localStorage sanity cap; the real desktop
// drag limit is computed dynamically in ChatApp from the viewport and sidebar state.
const PANEL_WIDTH_DEFAULT = 520;
const PANEL_WIDTH_MIN = 320;
const PANEL_WIDTH_MAX = 2400;

// the right dock went from always "files" to two
// tabs (Files | App); the currently selected tab is persisted alongside the panel prefs. Only these
// two known values are accepted; anything else (missing / stale localStorage) falls back to "files",
// so a dirty value can never strand the panel on a nonexistent tab.
const PANEL_TYPES = new Set(["files", "app"]);
const PANEL_TYPE_DEFAULT = "files";

function storedPanelPref() {
  try {
    const raw = window.localStorage.getItem(PANEL_PREFS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    const obj = parsed && typeof parsed === "object" ? parsed : {};
    const widthRaw = Number(obj.width);
    const width = Number.isFinite(widthRaw)
      ? Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, widthRaw))
      : PANEL_WIDTH_DEFAULT;
    // panelType: the currently selected right-dock tab ("files" | "app"); unknown values fall back to "files".
    const panelType = PANEL_TYPES.has(obj.panelType)
      ? obj.panelType
      : PANEL_TYPE_DEFAULT;
    // sidebar: the collapsed state of the left session column, also persisted with the panel prefs.
    return { open: !!obj.open, width, panelType, sidebar: !!obj.sidebar };
  } catch (error) {
    return {
      open: false,
      width: PANEL_WIDTH_DEFAULT,
      panelType: PANEL_TYPE_DEFAULT,
      sidebar: false,
    };
  }
}

function mergePanelPref(patch) {
  try {
    const next = { ...storedPanelPref(), ...patch };
    window.localStorage.setItem(PANEL_PREFS_KEY, JSON.stringify(next));
  } catch (error) {}
}

export {
  COMPOSER_PREFS_KEY,
  storedComposerPref,
  mergeComposerPref,
  PANEL_PREFS_KEY,
  PANEL_WIDTH_DEFAULT,
  PANEL_WIDTH_MIN,
  PANEL_WIDTH_MAX,
  PANEL_TYPE_DEFAULT,
  storedPanelPref,
  mergePanelPref,
};
