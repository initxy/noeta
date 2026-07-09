import { Monitor, PanelRightClose, Terminal, Code2 } from "lucide-react";
import { EmptyState } from "../components/EmptyState.jsx";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { FilePanel } from "./FilePanel.jsx";

// The right dock: a thin generic shell (drag handle + third column) with a
// lightweight tab bar on top. Built-in tabs:
//   Files — self-contained FilePanel (workspace tree)
//   App   — model-opened HTML artifact live in an <iframe>
// Sandbox preview tabs (only visible when the active session has a live
// sandbox container, i.e. previewInfo is non-null):
//   Browser — noVNC iframe (…/sandbox-preview/<token>/vnc/index.html?path=...)
//   Terminal — container PTY page (…/sandbox-preview/<token>/terminal)
//   Code    — code-server iframe (…/sandbox-preview/<token>/code-server/)
// Panel sub-paths come from the backend (previewInfo.panels) — the gateway
// owns the pinned container paths; the || fallbacks are last-resort only.
// The preview iframes are served from a DEDICATED origin (same hostname,
// previewInfo.port) — they need allow-same-origin (noVNC localStorage,
// code-server service worker), and that flag makes iframe content
// same-origin with its server. Serving container-controlled pages from the
// noeta origin would hand a compromised container the noeta control API,
// so the preview origin is a separate port that holds no noeta state.
// Push-style on wide screens (the grid 3rd track narrows .chat-main); the
// .app-shell media query degrades it to an overlay drawer on narrow screens.
// Presentational only — all panel/app state lives in ChatApp and arrives as
// props (the shell decides when to mount this via `panelMounted`).
function RightDock({
  activeTaskId,
  appReloadKey,
  appUrl,
  discoverTaskFiles,
  events,
  onClose,
  onOpenImage,
  onPanelResizeStart,
  onSelectPanelType,
  panelRefreshKey,
  panelType,
  previewInfo,
  readTaskFile,
  working,
}) {
  // The panels live on the dedicated preview origin, never the main port.
  // No port in the payload (gateway not fully wired) ⇒ no panels — falling
  // back to a main-port path would silently reopen the origin-isolation hole.
  const hasPreview = !!(previewInfo && previewInfo.token && previewInfo.port);
  const previewBase = hasPreview
    ? `${window.location.protocol}//${window.location.hostname}:${
        previewInfo.port
      }/sandbox-preview/${encodeURIComponent(previewInfo.token)}/`
    : "";

  return (
    <>
      <div
        className="panel-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize files panel"
        onPointerDown={onPanelResizeStart}
      />
      <aside className="right-dock" aria-label="Right dock">
        <div className="right-dock__tabs" role="tablist" aria-label="Panel">
          <button
            type="button"
            role="tab"
            aria-selected={panelType === "files"}
            className={`right-dock__tab${
              panelType === "files" ? " is-active" : ""
            }`}
            onClick={() => onSelectPanelType("files")}
          >
            Files
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={panelType === "app"}
            className={`right-dock__tab${
              panelType === "app" ? " is-active" : ""
            }`}
            onClick={() => onSelectPanelType("app")}
          >
            App
          </button>
          {hasPreview ? (
            <>
              <span className="right-dock__tab-divider" />
              <button
                type="button"
                role="tab"
                aria-selected={panelType === "browser"}
                className={`right-dock__tab${
                  panelType === "browser" ? " is-active" : ""
                }`}
                onClick={() => onSelectPanelType("browser")}
                title="Sandbox browser (noVNC)"
              >
                <Monitor size={ICON_SM} />
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={panelType === "terminal"}
                className={`right-dock__tab${
                  panelType === "terminal" ? " is-active" : ""
                }`}
                onClick={() => onSelectPanelType("terminal")}
                title="Sandbox terminal"
              >
                <Terminal size={ICON_SM} />
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={panelType === "code"}
                className={`right-dock__tab${
                  panelType === "code" ? " is-active" : ""
                }`}
                onClick={() => onSelectPanelType("code")}
                title="Sandbox code editor"
              >
                <Code2 size={ICON_SM} />
              </button>
            </>
          ) : null}
          <span className="right-dock__tabs-spacer" />
          <button
            type="button"
            className="icon-button"
            title="Close panel"
            aria-label="Close panel"
            onClick={onClose}
          >
            <PanelRightClose size={ICON_LG} />
          </button>
        </div>
        {/* Keep ALL mounted, toggling visibility, so switching tabs never
            tears down the iframe (which would reload the app/browser/code
            session) nor the file tree's expand/selection state. */}
        <div
          className="right-dock__pane"
          hidden={panelType !== "files"}
          aria-hidden={panelType !== "files"}
        >
          <FilePanel
            taskId={activeTaskId}
            discoverTaskFiles={discoverTaskFiles}
            readTaskFile={readTaskFile}
            refreshKey={panelRefreshKey}
            events={events}
            working={working}
            onClose={onClose}
            onOpenImage={onOpenImage}
          />
        </div>
        <div
          className="right-dock__pane"
          hidden={panelType !== "app"}
          aria-hidden={panelType !== "app"}
        >
          {appUrl ? (
            <iframe
              key={appReloadKey}
              className="app-iframe"
              src={appUrl}
              title="App preview"
              sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"
            />
          ) : (
            <EmptyState kind="app" title="No app open yet" />
          )}
        </div>
        {hasPreview ? (
          <>
            <div
              className="right-dock__pane"
              hidden={panelType !== "browser"}
              aria-hidden={panelType !== "browser"}
            >
              <iframe
                className="sandbox-preview-iframe"
                src={`${previewBase}${previewInfo.panels?.browser || "vnc/"}`}
                title="Sandbox browser"
                sandbox="allow-scripts allow-forms allow-modals allow-popups allow-same-origin"
              />
            </div>
            <div
              className="right-dock__pane"
              hidden={panelType !== "terminal"}
              aria-hidden={panelType !== "terminal"}
            >
              <iframe
                className="sandbox-preview-iframe"
                src={`${previewBase}${previewInfo.panels?.terminal || "terminal"}`}
                title="Sandbox terminal"
                sandbox="allow-scripts allow-forms allow-modals allow-same-origin"
              />
            </div>
            <div
              className="right-dock__pane"
              hidden={panelType !== "code"}
              aria-hidden={panelType !== "code"}
            >
              <iframe
                className="sandbox-preview-iframe"
                src={`${previewBase}${previewInfo.panels?.code || "code-server/"}`}
                title="Sandbox code editor"
                sandbox="allow-scripts allow-forms allow-modals allow-popups allow-same-origin allow-downloads"
              />
            </div>
          </>
        ) : null}
      </aside>
      <div className="right-dock-overlay" aria-hidden="true" onClick={onClose} />
    </>
  );
}

export { RightDock };
