import { PanelRightClose } from "lucide-react";
import { EmptyState } from "../components/EmptyState.jsx";
import { ICON_LG } from "../shared/icons.js";
import { FilePanel } from "./FilePanel.jsx";

// The right dock: a thin generic shell (drag handle + third column) with a
// lightweight `Files | App` tab bar on top. "Files" hosts the self-contained
// FilePanel; "App" renders the model's HTML artifact live in an <iframe>.
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
  readTaskFile,
  working,
}) {
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
        {/* Keep BOTH mounted, toggling visibility, so switching tabs never
            tears down the iframe (which would reload the app) nor the file
            tree's expand/selection state. */}
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
              // key bump forces a remount = reload when the
              // model edits a file under the mounted dir.
              key={appReloadKey}
              className="app-iframe"
              src={appUrl}
              title="App preview"
              // single-port revision red line — the preview is now served from
              // noeta's OWN origin (/preview/<token>/), so the iframe is
              // same-origin with the UI. Isolation therefore rides ONLY on
              // sandbox: every flag below is safe in an opaque/null origin —
              // the app can run JS, submit forms, open dialogs/popups, and
              // download, but still cannot touch the noeta UI, its cookies, or
              // storage. The ONE flag we must NEVER add is `allow-same-origin`:
              // since the iframe is same-origin with noeta, that would drop the
              // null-origin wall and open an XSS hole into the UI. The app's
              // /api fetch is null-origin → answered with permissive CORS by
              // the gateway.
              sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"
            />
          ) : (
            <EmptyState kind="app" title="No app open yet" />
          )}
        </div>
      </aside>
      {/* U16 — on narrow screens the dock becomes a fixed overlay drawer; this
          scrim sits just under it (z-index 39) so tapping the chat behind it
          closes the dock and signals the modal state. Hidden on wide screens
          via CSS (display:none), so it never covers the pushed-column layout. */}
      <div className="right-dock-overlay" aria-hidden="true" onClick={onClose} />
    </>
  );
}

export { RightDock };
