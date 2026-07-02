import { useEffect } from "react";

// Fallback permission menu used until GET /capabilities resolves. The live list
// from the host supersedes it.
const FALLBACK_PERMISSIONS = ["default", "acceptEdits", "bypassPermissions"];

// U3 — the permission chip showed the raw backend API value (default /
// acceptEdits / bypassPermissions) with no explanation. Map each to a readable
// name + a hover tooltip that spells out what it actually does. Unknown values
// fall back to the raw name.
const PERMISSION_META = {
  default: {
    label: "Default",
    hint: "High-risk actions (editing files / running commands / deleting) ask first",
  },
  acceptEdits: {
    label: "Accept edits",
    hint: "File writes no longer ask; running commands / deleting still ask",
  },
  bypassPermissions: {
    label: "Bypass",
    hint: "Edits, deletes, and commands run without asking — use only for trusted tasks",
  },
};
const permissionLabel = (value) => PERMISSION_META[value]?.label || value;
// B8 — humanise a raw backend status for display only (logic still keys off the
// raw value). Unknown and closed get friendly labels; everything else (running,
// terminal, …) passes through unchanged.
function humanSessionStatus(status) {
  const value = String(status || "").toLowerCase();
  if (!value || value === "unknown") return "Active";
  if (value === "closed") return "Closed";
  return status;
}

// B11 — a dispatcher status that failed / errored / was cancelled paints the dot
// red, overriding an otherwise-green terminal state so "session crashed" is
// visually distinct from "session finished normally".
function sessionDotClass(status, dispatcherStatus) {
  const disp = String(dispatcherStatus || "").toLowerCase();
  if (disp.includes("fail") || disp.includes("error") || disp.includes("cancel")) {
    return "failed";
  }
  const value = String(status || "").toLowerCase();
  if (value.includes("closed")) return "closed";
  if (value.includes("run")) return "running";
  if (value.includes("ready") || value.includes("created")) return "ready";
  if (value.includes("fail") || value.includes("error") || value.includes("cancel")) {
    return "failed";
  }
  if (value.includes("approval") || value.includes("question")) return "attention";
  if (
    value.includes("wait") ||
    value.includes("suspend") ||
    value.includes("terminal") ||
    value.includes("complete") ||
    value.includes("done")
  ) {
    return "done";
  }
  return "";
}
// Shared dismiss behaviour for the composer's inline popovers (codex bottom
// bar): close on an outside pointerdown or Escape while the popover is open.
function usePopoverDismiss(open, onDismiss, rootRef) {
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) onDismiss();
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") onDismiss();
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open, onDismiss, rootRef]);
}

export {
  FALLBACK_PERMISSIONS,
  PERMISSION_META,
  permissionLabel,
  humanSessionStatus,
  sessionDotClass,
  usePopoverDismiss,
};
