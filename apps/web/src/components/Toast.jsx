// U12 — fixed-position toast stack (replaces the inline Banner that was pinned
// absolute at top:58px and overlapped a tall header). The queue + auto-dismiss
// timers live in chat-data (useChatData); this only renders. Each toast carries
// { id, kind, text, action? }: info / loading / success auto-dismiss; error
// stays until the × is clicked. `action` = { label, onClick } renders a button
// (U5 batch undo, B4 copy retry) — clicking it runs onClick then dismisses.

import { X } from "lucide-react";

function ToastStack({ toasts, onDismiss }) {
  if (!toasts || !toasts.length) return null;
  return (
    <div
      className="toast-stack"
      role="region"
      aria-label="Notifications"
      aria-live="polite"
    >
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.kind || "info"}`} role="status">
          {toast.kind === "loading" ? (
            <span className="toast__spinner" aria-hidden="true" />
          ) : null}
          <span className="toast__text">{toast.text}</span>
          {toast.action ? (
            <button
              type="button"
              className="toast__action"
              onClick={() => {
                toast.action.onClick?.();
                onDismiss?.(toast.id);
              }}
            >
              {toast.action.label}
            </button>
          ) : null}
          <button
            type="button"
            className="toast__close"
            aria-label="Dismiss notification"
            onClick={() => onDismiss?.(toast.id)}
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

export { ToastStack };
