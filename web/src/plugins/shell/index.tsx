// The shell plugin's web part: a `Bash` (or `Monitor`) call's card shows the
// command, its output as it streams and once done; the approval card shows
// the command it asks about.
import { ToolOutput } from "@noeta-ai/ui";
import { requestSlot, toolSlot, type PluginContext, type RequestSlotProps, type ToolSlotProps } from "@noeta-ai/web-sdk";

/** The tools that run a command. */
export const COMMANDS = ["Bash", "Monitor"];

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

function Command({ call }: ToolSlotProps) {
  // A Monitor of a WebSocket has no command: its address, as the card's head names it.
  const command = str(call.input.command ?? (call.input.ws as { url?: unknown } | undefined)?.url);
  return (
    <>
      <pre className="command">$ {command}</pre>
      {call.progress && call.status === "running" && <ToolOutput text={call.progress} />}
      {call.result !== undefined && <ToolOutput text={call.result} error={call.status === "error"} />}
    </>
  );
}

function Asked({ request }: RequestSlotProps) {
  return <pre className="command">$ {str(request.input.command)}</pre>;
}

export function activate(ctx: PluginContext): void {
  for (const tool of COMMANDS) ctx.slots.add(toolSlot(tool), { mount: "single" }, Command);
  ctx.slots.add(requestSlot("Bash"), {}, Asked);
}
