import { ChevronDown, Wrench } from "lucide-react";
import { useState } from "react";
import { cn } from "../../lib/classnames.js";
import { safeJson } from "../../lib/format.js";

const statusLabels = {
  "approval-requested": "Awaiting Approval",
  "approval-responded": "Responded",
  "input-available": "Running",
  "input-streaming": "Pending",
  "output-available": "Completed",
  "output-denied": "Denied",
  "output-error": "Error",
};

function Tool({ className, children, defaultOpen = false, ...props }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details
      className={cn("ai-tool", className)}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      {...props}
    >
      {children}
    </details>
  );
}

// Deliberately quiet: a small wrench, the tool name, a one-line argument hint,
// and a single status dot whose colour carries success/error. The verbose
// pill-badge was dropped so tool calls recede behind the assistant's prose —
// the full status word survives as the summary's title tooltip.
function ToolHeader({ title, state = "input-available", className, children }) {
  return (
    <summary className={cn("ai-tool-header", className)} title={statusLabels[state] || state}>
      <Wrench size={13} />
      <span className="ai-tool-title">{title || "tool"}</span>
      {children}
      <span className={cn("ai-tool-dot", `state-${state}`)} aria-hidden="true" />
      <ChevronDown className="ai-disclosure-icon" size={14} />
    </summary>
  );
}

function ToolContent({ className, children, ...props }) {
  return (
    <div className={cn("ai-tool-content", className)} {...props}>
      {children}
    </div>
  );
}

function ToolInput({ input, className, ...props }) {
  if (!input || (typeof input === "object" && Object.keys(input).length === 0)) {
    return null;
  }
  return (
    <section className={cn("ai-tool-section", className)} {...props}>
      <h4>Parameters</h4>
      <pre>{safeJson(input)}</pre>
    </section>
  );
}

function ToolOutput({ output, errorText, className, ...props }) {
  if (output == null && !errorText) return null;
  return (
    <section className={cn("ai-tool-section", className)} {...props}>
      <h4>{errorText ? "Error" : "Result"}</h4>
      <pre>{errorText || (typeof output === "string" ? output : safeJson(output))}</pre>
    </section>
  );
}

export { Tool, ToolContent, ToolHeader, ToolInput, ToolOutput };
