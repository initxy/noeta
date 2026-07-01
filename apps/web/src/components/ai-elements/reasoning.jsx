import { createContext, useContext, useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../../lib/classnames.js";

// U11 — the reasoning block owns all of its own state (char count, elapsed
// timer, rail phase, open/close). The trigger renders the title but the char
// count comes from the content's children, so the two share state through a
// context rather than props threaded down from ChatApp.
const ReasoningContext = createContext(null);

function textLength(children) {
  if (typeof children === "string") return children.length;
  if (Array.isArray(children)) {
    return children.reduce(
      (n, c) => n + (typeof c === "string" ? c.length : 0),
      0,
    );
  }
  return 0;
}

function formatElapsed(seconds) {
  // cap the displayed value: past 5 minutes the exact second stops mattering
  if (seconds > 300) return "5min+";
  return `${seconds}s`;
}

function Reasoning({ className, isStreaming, isFirst = false, children, ...props }) {
  const [charCount, setCharCount] = useState(0);
  const [open, setOpen] = useState(Boolean(isFirst));
  const [elapsed, setElapsed] = useState(0);
  // rail phase: false = "active" (warning + breathing), true = "done" (success).
  // One-way latch — once the content has settled it never goes back to active.
  const [railDone, setRailDone] = useState(false);

  const startRef = useRef(Date.now());
  const detailsRef = useRef(null);
  const userToggled = useRef(false); // suppress auto-collapse after a manual toggle
  const programmatic = useRef(false); // distinguish our own setOpen from user clicks

  // First reasoning of the session opens itself, then folds back after 2s unless
  // the reader has already reached for it. (ChatApp does not pass isFirst yet, so
  // this stays dormant — every block defaults to collapsed.)
  useEffect(() => {
    if (!isFirst) return undefined;
    const timer = setTimeout(() => {
      if (userToggled.current) return;
      programmatic.current = true;
      setOpen(false);
    }, 2000);
    return () => clearTimeout(timer);
  }, [isFirst]);

  // Rail turns green ("done") either after 2s, or 500ms after the content stops
  // resizing — whichever lands first. The content is already complete on render
  // (no token stream), so the ResizeObserver debounce is what fires in practice.
  useEffect(() => {
    const cap = setTimeout(() => setRailDone(true), 2000);
    let settle;
    let observer;
    const el = detailsRef.current;
    if (el && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        clearTimeout(settle);
        settle = setTimeout(() => setRailDone(true), 500);
      });
      observer.observe(el);
    }
    return () => {
      clearTimeout(cap);
      clearTimeout(settle);
      if (observer) observer.disconnect();
    };
  }, []);

  // Elapsed only ticks while open — a collapsed block shows just the char count,
  // so there is no per-second repaint of a resting transcript.
  useEffect(() => {
    if (!open) return undefined;
    setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [open]);

  const onToggle = (event) => {
    const next = event.currentTarget.open;
    if (programmatic.current) {
      programmatic.current = false;
    } else {
      userToggled.current = true;
    }
    setOpen(next);
  };

  const ctx = { open, charCount, elapsed, setCharCount };
  return (
    <ReasoningContext.Provider value={ctx}>
      <details
        ref={detailsRef}
        className={cn(
          "ai-reasoning",
          railDone ? "rail-done" : "rail-active",
          isStreaming && "is-streaming",
          className,
        )}
        open={open}
        onToggle={onToggle}
        {...props}
      >
        {children}
      </details>
    </ReasoningContext.Provider>
  );
}

function ReasoningTrigger({ children, meta: _meta }) {
  const ctx = useContext(ReasoningContext);
  const open = ctx?.open ?? false;
  const charCount = ctx?.charCount ?? 0;
  const elapsed = ctx?.elapsed ?? 0;
  return (
    <summary className="ai-reasoning-trigger">
      <span className="ai-reasoning-emoji" aria-hidden="true">
        🧠
      </span>
      <span>{children || "Reasoning"}</span>
      <span className="ai-reasoning-meta">
        {` · ${charCount.toLocaleString()} chars`}
        {open ? ` · ${formatElapsed(elapsed)} elapsed` : ""}
      </span>
      <ChevronDown className="ai-disclosure-icon" size={13} />
    </summary>
  );
}

function ReasoningContent({ className, children, ...props }) {
  const ctx = useContext(ReasoningContext);
  const setCharCount = ctx?.setCharCount;
  useEffect(() => {
    if (!setCharCount) return;
    setCharCount(textLength(children));
  }, [children, setCharCount]);
  return (
    <div className={cn("ai-reasoning-content", className)} {...props}>
      {children}
    </div>
  );
}

export { Reasoning, ReasoningContent, ReasoningTrigger };
