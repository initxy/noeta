import { Copy, PanelRight } from "lucide-react";
import { copyText } from "../lib/clipboard.js";
import { shortId } from "../lib/format.js";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { humanSessionStatus, sessionDotClass } from "./chat-shared.js";

function ChatHeader({
  activeDetail,
  activeTaskId,
  commandIn,
  model,
  onReopen,
  openingSession,
  panelOpen,
  onTogglePanel,
  pushToast,
  vm,
}) {
  const parts = [];
  // Brief seed window: the POST /tasks round-trip before task_id comes back.
  // With async turns this is milliseconds, then the real folded status takes
  // over (the task is created + the turn is already running on the server).
  if (!activeTaskId && openingSession) parts.push("starting…");
  if (activeTaskId) {
    // B8 — humanise the leading status token; the raw value still drives the dot.
    parts.push(
      humanSessionStatus(
        activeDetail?.status_text || activeDetail?.status || vm.status || "unknown",
      ),
    );
    if (activeDetail?.closed) parts.push("Closed");
    if (
      activeDetail?.dispatcher_status &&
      activeDetail.dispatcher_status !== activeDetail.status
    ) {
      parts.push(`Dispatcher ${humanSessionStatus(activeDetail.dispatcher_status)}`);
    }
    if (activeDetail?.approval_call_id) parts.push(`approval ${activeDetail.approval_call_id}`);
    if (activeDetail?.question_id) parts.push(`question ${activeDetail.question_id}`);
  }
  const statusLabel = parts.join(" · ") || "Session";
  const statusDot = activeTaskId
    ? activeDetail?.closed
      ? "closed"
      : activeDetail?.wake_kind === "approval" ||
          activeDetail?.wake_kind === "question"
        ? "attention"
        : activeDetail?.status_text || activeDetail?.status || vm.status
    : openingSession
      ? "running"
      : "unknown";
  return (
    <header className="chat-header">
      <span className="chat-header__title">
        {activeTaskId
          ? activeDetail?.title || shortId(activeTaskId)
          : "New session"}
      </span>
      <span
        className={`chat-header__status-dot session-dot ${sessionDotClass(
          statusDot,
          activeTaskId ? activeDetail?.dispatcher_status : null,
        )}`}
        title={statusLabel}
        aria-label={statusLabel}
      />
      {model ? <span className="chat-header__model">{model}</span> : null}
      <div className="chat-header__spacer" />
      {activeTaskId ? (
        <>
          <button
            className="icon-button"
            type="button"
            title="Copy trace id"
            aria-label="Copy trace id"
            onClick={async () => {
              const ok = await copyText(activeTaskId);
              pushToast?.(
                ok ? "success" : "error",
                ok ? "Copied" : "Copy failed — select manually",
              );
            }}
          >
            <Copy size={ICON_SM} />
          </button>
          <a
            className="trace-link"
            href={`/trace?task=${encodeURIComponent(activeTaskId)}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Trace
          </a>
        </>
      ) : null}
      {/* the file panel toggle. Disabled with no active session
          (the panel is task-scoped: no target workspace_dir). */}
      <button
        type="button"
        className={`icon-button file-panel-toggle${panelOpen ? " is-active" : ""}`}
        disabled={!activeTaskId}
        aria-pressed={!!panelOpen}
        title={panelOpen ? "Hide files" : "Show files"}
        aria-label={panelOpen ? "Hide files" : "Show files"}
        onClick={onTogglePanel}
      >
        <PanelRight size={ICON_LG} />
      </button>
    </header>
  );
}

export { ChatHeader };
