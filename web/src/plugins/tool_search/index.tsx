// The tool-search plugin's web part: a `ToolSearch` call is one line, what
// it found once opened.
import { ToolOutput } from "@initxy/ui";
import { toolSlot, type PluginContext, type ToolSlotProps } from "@initxy/web-sdk";

function Search({ call, open }: ToolSlotProps) {
  return open && call.result !== undefined ? <ToolOutput text={call.result} error={call.status === "error"} open /> : null;
}

export function activate(ctx: PluginContext): void {
  ctx.slots.add(toolSlot("ToolSearch"), { mount: "single" }, Search);
}
