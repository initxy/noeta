import { memo, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Item } from "../state/transcript";
import { ToolOutput } from "@initxy/noeta-ui";
import { Slot, toolSlot, useSlotCount, type ToolCall, type ToolSlotProps } from "@initxy/noeta-web-sdk";

type Tool = Extract<Item, { kind: "tool" }>;

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

const TARGETS = ["file_path", "path", "command", "pattern", "url", "query", "description", "subject"];
const PATHS = new Set(["file_path", "path"]);

/** The call's main argument, as Claude Code's tool lines name it: a path
 * shortened to its last two parts, else the first line of a command, a
 * pattern, an address … */
export function toolTarget(input: Record<string, unknown>): string {
  const key = TARGETS.find((k) => input[k] != null);
  if (key === undefined) return "";
  const first = str(input[key]).split("\n")[0];
  return PATHS.has(key) ? first.replace(/^.*\/(?=[^/]+\/[^/]+$)/, "…/") : first;
}

function StatusDot({ status }: { status: Tool["status"] }) {
  return <span className={`dot dot-${status}`} aria-label={status} />;
}

/** A tool no plugin gives a view of: its input once opened, its output. */
function Generic({ call, open }: ToolSlotProps) {
  return (
    <>
      {open && <pre className="input">{JSON.stringify(call.input, null, 2)}</pre>}
      {call.result !== undefined && <ToolOutput text={call.result} error={call.status === "error"} />}
    </>
  );
}

/** One tool call: its name and target, its state, and its body — the
 * `tool:<Tool>` slot, where the plugin that offers the tool shows the view
 * the tool deserves (a command and its output, a diff, a sub-agent's work).
 * A card whose plugin shows nothing while it is closed is drawn as one
 * line, with how long its result is. */
export const ToolCard = memo(function ToolCard({ t }: { t: Tool }) {
  const [open, setOpen] = useState(false);
  const slot = toolSlot(t.name);
  const contributed = useSlotCount(slot) > 0;
  const body = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);
  // Whether the plugin's view is empty is known once drawn; an opened card
  // keeps the look it had closed.
  const call: ToolCall = useMemo(
    () => ({ id: t.id, name: t.name, input: t.input, status: t.status, result: t.result, meta: t.meta, progress: t.progress, children: t.children }),
    [t],
  );
  useLayoutEffect(() => {
    if (!open) setCompact(contributed && body.current !== null && body.current.childNodes.length === 0);
  }, [open, contributed, call]);
  const mcp = t.name.startsWith("mcp__");
  const label = mcp ? t.name.slice(5).split("__").join(" · ") : t.name;
  const summary = compact && t.result !== undefined && !open ? `${t.result.split("\n").length} lines` : "";
  return (
    <div className={`tool tool-${compact ? "compact" : "full"}`} data-tool={t.name} data-status={t.status}>
      <div className="tool-head" onClick={() => setOpen(!open)}>
        <StatusDot status={t.status} />
        <span className="tool-name">{label}</span>
        <span className="tool-target">{toolTarget(t.input)}</span>
        {summary && <span className="muted"> · {summary}</span>}
        {t.status === "waiting" && <span className="badge">waiting for you</span>}
      </div>
      {/* The generic view is the slot's own content: a plugin's `single` view replaces it. */}
      <div className="tool-body" ref={body}>
        <Slot name={slot} props={{ call, open }}>
          <Generic call={call} open={open} />
        </Slot>
      </div>
      {t.note && <div className="tool-note">⎿ {t.note}</div>}
    </div>
  );
});
