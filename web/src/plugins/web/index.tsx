// The web plugin's web part: a `WebFetch` call's card, opened, shows what
// was asked of the page (its prompt) rather than the raw input.
import { ToolOutput } from "@initxy/ui";
import { toolSlot, type PluginContext, type ToolSlotProps } from "@initxy/web-sdk";

function Fetch({ call, open }: ToolSlotProps) {
  return (
    <>
      {open && <pre className="input">{typeof call.input.prompt === "string" ? call.input.prompt : ""}</pre>}
      {call.result !== undefined && <ToolOutput text={call.result} error={call.status === "error"} />}
    </>
  );
}

export function activate(ctx: PluginContext): void {
  ctx.slots.add(toolSlot("WebFetch"), { mount: "single" }, Fetch);
}
