import { Bot, Terminal, Workflow, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { EmptyState } from "../components/EmptyState.jsx";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { usePopoverDismiss } from "./chat-shared.js";
import { Transcript, commandPreview, friendlyAgentName } from "./Transcript.jsx";

// Drill-in for a background job: reuses the trace inspector's deref-an-artifact
// overlay pattern (.preview-overlay/.preview-dialog) to render the LATEST
// recorded output snapshot behind the job's ``ref``. A running job shows its
// most recent polled snapshot (no live tail — out of scope); a
// terminal job shows the final output. The ref is a content-ref, so it derefs
// through the task-scoped ``/tasks/{id}/content/{hash}`` endpoint, exactly like
// PreviewModal's content branch.
function BackgroundJobModal({ job, taskId, onClose }) {
  const [state, setState] = useState({ status: "idle" });
  const hash = job?.ref?.hash || null;

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // U10 — return focus to whatever opened the modal (the trigger chip) on close,
  // so keyboard users aren't dumped on <body>. Captured once on mount, restored
  // on unmount; empty deps so a parent re-render can't disturb the captured trigger.
  useEffect(() => {
    const trigger = document.activeElement;
    return () => {
      if (trigger && typeof trigger.focus === "function") {
        trigger.focus({ preventScroll: true });
      }
    };
  }, []);

  useEffect(() => {
    if (!hash || !taskId) {
      setState({ status: "empty" });
      return undefined;
    }
    let cancelled = false;
    setState({ status: "loading" });
    // New protocol: blobs deref from the global, content-addressed
    // GET /content/{hash} which returns RAW bytes (not a {text} JSON wrapper).
    fetch(`/content/${encodeURIComponent(hash)}`)
      .then(async (res) => {
        if (cancelled) return;
        const text = res.ok ? await res.text() : `error: HTTP ${res.status}`;
        setState({ status: "ok", text });
      })
      .catch((error) => {
        if (!cancelled) {
          setState({ status: "ok", text: `error: ${error.message || "fetch failed"}` });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [hash, taskId]);

  const statusText =
    job.status === "killed"
      ? `killed${job.signal ? ` · signal ${job.signal}` : ""}`
      : job.status === "exited"
        ? `exited${typeof job.exitCode === "number" ? ` · code ${job.exitCode}` : ""}`
        : job.status === "lost"
          ? "lost"
          : "running";

  return createPortal(
    <div className="preview-overlay" role="presentation" onClick={onClose}>
      <div
        className="preview-dialog background-job-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={job.command}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="preview-head">
          <Terminal size={ICON_SM} />
          <span className="preview-title">{commandPreview(job.command)}</span>
          <span className="preview-sub">{statusText}</span>
          <div className="preview-actions">
            <button className="preview-close" type="button" title="Close (Esc)" onClick={onClose}>
              <X size={ICON_LG} />
            </button>
          </div>
        </header>
        <div className="preview-body">
          {state.status === "loading" ? (
            <EmptyState kind="pane" title="Loading…" />
          ) : state.status === "empty" ? (
            <EmptyState kind="pane" title="No output yet" />
          ) : (
            <pre className="trace-payload preview-pre">{state.text || ""}</pre>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

// Find a workflow node's authored script (the real "prompt" for a __workflow__
// subtask) from its own TaskCreated.inputs.
function workflowScriptFromEvents(events) {
  for (const env of events) {
    if (env && env.type === "TaskCreated") {
      const script = env.payload?.inputs?.script;
      if (typeof script === "string") return script;
    }
  }
  return null;
}

// U10 — the breadcrumb path. Short paths render every crumb; once the stack is
// deeper than 3, the middle layers (indices 1..-2) collapse into a single "…"
// chip that opens a dropdown to jump to any of them. This keeps the header on ONE
// row so the `.subtask-status` pill on the right never gets pushed out of view.
function SubtaskCrumbs({ stack, onCrumb }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  usePopoverDismiss(open, close, rootRef);
  const crumbIcon = (node) =>
    node.agentName === "__workflow__" ? (
      <Workflow size={ICON_SM} />
    ) : (
      <Bot size={ICON_SM} />
    );
  const renderCrumb = (node, index) => (
    <button
      className="crumb"
      disabled={index === stack.length - 1}
      type="button"
      onClick={() => onCrumb(index)}
    >
      {crumbIcon(node)}
      {friendlyAgentName(node.agentName)}
    </button>
  );
  const sep = <span className="crumb-sep">›</span>;
  if (stack.length <= 3) {
    return (
      <div className="subtask-crumbs">
        {stack.map((node, index) => (
          <Fragment key={node.taskId}>
            {index > 0 ? sep : null}
            {renderCrumb(node, index)}
          </Fragment>
        ))}
      </div>
    );
  }
  const middle = stack.slice(1, -1);
  const lastIndex = stack.length - 1;
  return (
    <div className="subtask-crumbs" ref={rootRef}>
      {renderCrumb(stack[0], 0)}
      {sep}
      <div className="crumb-collapse">
        <button
          type="button"
          className="crumb crumb-more"
          aria-haspopup="menu"
          aria-expanded={open}
          title="Show hidden levels"
          onClick={() => setOpen((value) => !value)}
        >
          …
        </button>
        {open ? (
          <div className="crumb-menu" role="menu">
            {middle.map((node, offset) => {
              const index = offset + 1;
              return (
                <button
                  key={node.taskId}
                  type="button"
                  className="crumb-menu__item"
                  onClick={() => {
                    onCrumb(index);
                    close();
                  }}
                >
                  {crumbIcon(node)}
                  {friendlyAgentName(node.agentName)}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
      {sep}
      {renderCrumb(stack[lastIndex], lastIndex)}
    </div>
  );
}

// The subtask drill-in popup: a breadcrumb (the workflow tree path) + the
// task prompt / workflow script pinned at the top + the SAME transcript
// renderer pointed at the subtask's own event stream. Reuses the .preview-*
// overlay/dialog the trace inspector already ships.
function SubtaskModal({
  detail,
  ensureMessageBodiesBatch,
  ensureThinkingBatch,
  events,
  localImageCache,
  onOpenImage,
  messageFullCache,
  messageTextCache,
  onClose,
  onCrumb,
  onDrill,
  responseThinkingCache,
  stack,
  vm,
}) {
  const top = stack[stack.length - 1] || {};
  const taskId = top.taskId || null;
  const isWorkflow = top.agentName === "__workflow__";
  const script = isWorkflow ? workflowScriptFromEvents(events) : null;
  const prompt = isWorkflow ? null : top.goal || detail?.goal || null;
  const statusText = detail?.status_text || detail?.status || vm.status || "";
  // B9 — Esc closes the drawer, symmetric with BackgroundJobModal (keyboard
  // users had no way to dismiss it but the mouse-only overlay click). Topmost
  // guard: an image Lightbox can open OVER this drawer (onOpenImage), and it
  // owns its own Esc; while it is up, let Esc close only the lightbox so one
  // keypress never collapses both layers.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      if (document.querySelector(".lightbox-overlay")) return;
      onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  // U10 — symmetric with BackgroundJobModal: return focus to the trigger chip on
  // close so keyboard users aren't dumped on <body>. Captured once on mount.
  useEffect(() => {
    const trigger = document.activeElement;
    return () => {
      if (trigger && typeof trigger.focus === "function") {
        trigger.focus({ preventScroll: true });
      }
    };
  }, []);
  return createPortal(
    <div className="preview-overlay" onClick={onClose}>
      <div
        className="preview-dialog subtask-dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="preview-head">
          <SubtaskCrumbs stack={stack} onCrumb={onCrumb} />
          <span className="subtask-status">{statusText}</span>
          <button className="preview-close" type="button" onClick={onClose}>
            <X size={ICON_LG} />
          </button>
        </div>
        <div className="preview-body">
          {script ? (
            <details className="subtask-prompt" open>
              <summary>Workflow script</summary>
              <pre className="subtask-script">{script}</pre>
            </details>
          ) : prompt ? (
            <details className="subtask-prompt" open>
              <summary>Task prompt</summary>
              <div className="subtask-prompt-body">{prompt}</div>
            </details>
          ) : null}
          <Transcript
            activeTaskId={taskId}
            events={events}
            ensureMessageBodiesBatch={ensureMessageBodiesBatch}
            ensureThinkingBatch={ensureThinkingBatch}
            localImageCache={localImageCache}
            onOpenImage={onOpenImage}
            messageFullCache={messageFullCache}
            messageTextCache={messageTextCache}
            onOpenSubtask={onDrill}
            responseThinkingCache={responseThinkingCache}
            vm={vm}
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}

export { BackgroundJobModal, SubtaskModal };
