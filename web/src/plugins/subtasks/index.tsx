// The subtasks plugin's web part: an `Agent` call's card names the agent and
// its job, and opens the sub-agent's own conversation, followed live.
import { ToolOutput } from "@initxy/ui";
import { toolSlot, type PluginContext, type ToolSlotProps } from "@initxy/web-sdk";
import { useState, type ComponentType } from "react";

/** `Agent`, and `Task`, its old name. */
export const TOOLS = ["Agent", "Task"];

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

export function SubAgent({ child, Transcript }: { child?: { id: string; status?: string }; Transcript: ComponentType<{ taskId: string }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="subagent">
      <button className="link" onClick={() => setOpen(!open)} disabled={!child}>
        {open ? "Hide" : "Show"} the sub-agent's work{child?.status ? ` (${child.status})` : ""}
      </button>
      {open && child && <Transcript taskId={child.id} />}
    </div>
  );
}

export function activate(ctx: PluginContext): void {
  const Card = ({ call }: ToolSlotProps) => (
    <>
      <div className="muted">
        {str(call.input.subagent_type) || "general-purpose"} · {str(call.input.description)}
      </div>
      <SubAgent child={call.children?.[call.children.length - 1]} Transcript={ctx.Transcript} />
      {call.result !== undefined && call.status === "error" && <ToolOutput text={call.result} error />}
    </>
  );
  for (const tool of TOOLS) ctx.slots.add(toolSlot(tool), { mount: "single" }, Card);
}
