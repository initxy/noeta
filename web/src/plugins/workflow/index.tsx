// The workflow plugin's web part: a `Workflow` call's card follows its run
// (the plugin's `run`, `agent` and `ended` records): the workflow's name, how
// the run stands, and each agent it started, whose conversation opens.
import { ToolOutput } from "@noeta-ai/ui";
import { toolSlot, useAllRecords, type PluginContext, type PluginRecord, type ToolSlotProps } from "@noeta-ai/web-sdk";
import { useState, type ComponentType } from "react";

const TOOL = "Workflow";

export interface Run {
  id: string;
  name: string;
  description: string;
  status: string;
  error: string;
  agents: { child: string; label: string; phase: string }[];
}

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

/** The run the call with `callId` started, from the plugin's records. */
export function runOf(records: PluginRecord[], callId: string): Run | null {
  let run = null as Run | null;
  for (const { kind, data } of records) {
    const d = (data ?? {}) as Record<string, unknown>;
    if (kind === "run" && d.call_id === callId) {
      run = { id: str(d.run_id), name: str(d.name), description: str(d.description), status: "running", error: "", agents: [] };
    } else if (run && d.run_id === run.id && kind === "agent") {
      run = { ...run, agents: [...run.agents, { child: str(d.child_id), label: str(d.label), phase: str(d.phase) }] };
    } else if (run && d.run_id === run.id && kind === "ended") {
      run = { ...run, status: str(d.status), error: str(d.error) };
    }
  }
  return run;
}

function Agent({ agent, Transcript }: { agent: Run["agents"][number]; Transcript: ComponentType<{ taskId: string }> }) {
  const [open, setOpen] = useState(false);
  return (
    <li>
      <button className="link" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} {agent.label || "agent"}
        {agent.phase && <span className="muted"> · {agent.phase}</span>}
      </button>
      {open && <Transcript taskId={agent.child} />}
    </li>
  );
}

export function activate(ctx: PluginContext): void {
  const Card = ({ call }: ToolSlotProps) => {
    const run = runOf(useAllRecords(ctx), call.id);
    return (
      <>
        {run && (
          <div className="workflow-run">
            <div className="muted">
              {run.name || "Workflow"}
              {run.description && ` · ${run.description}`} · {run.status}
              {run.error && `: ${run.error}`}
            </div>
            {run.agents.length > 0 && (
              <ul className="workflow-agents" style={{ listStyle: "none", margin: "4px 0", padding: 0 }}>
                {run.agents.map((a) => (
                  <Agent key={a.child} agent={a} Transcript={ctx.Transcript} />
                ))}
              </ul>
            )}
          </div>
        )}
        {call.result !== undefined && <ToolOutput text={call.result} error={call.status === "error"} />}
      </>
    );
  };
  ctx.slots.add(toolSlot(TOOL), { mount: "single" }, Card);
}
