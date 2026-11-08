// The file tools' web part: an edit's card shows its change as a diff (the
// one it proposes, then the one recorded), the approval card the change it
// asks about; a `Read` is one line, its text once opened.
import { DiffView, proposedDiff, ToolOutput } from "@initxy/ui";
import { requestSlot, toolSlot, type PluginContext, type RequestSlotProps, type ToolSlotProps } from "@initxy/web-sdk";

/** The tools whose call is a change to a file. */
export const EDITS = ["Edit", "MultiEdit", "Write", "NotebookEdit"];

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/** The change an edit tool's call proposes, from its input. */
export function editDiff(name: string, input: Record<string, unknown>): string {
  if (name === "Write") return proposedDiff("", str(input.content));
  if (name === "Edit") return proposedDiff(str(input.old_string), str(input.new_string));
  if (name === "NotebookEdit") return proposedDiff("", str(input.new_source));
  if (name === "MultiEdit" && Array.isArray(input.edits)) {
    return (input.edits as { old_string?: string; new_string?: string }[]).map((e) => proposedDiff(str(e.old_string), str(e.new_string))).join("\n");
  }
  return "";
}

function Change({ call }: ToolSlotProps) {
  const done = call.meta?.diff;
  const diff = typeof done === "string" && done ? done : editDiff(call.name, call.input);
  const added = call.meta?.added as number | undefined;
  const removed = call.meta?.removed as number | undefined;
  return (
    <>
      {added !== undefined && (
        <div className="diffstat">
          <span className="add">+{added}</span> <span className="del">-{removed ?? 0}</span>
        </div>
      )}
      {diff && <DiffView diff={diff} />}
      {call.status === "error" && call.result && <ToolOutput text={call.result} error />}
    </>
  );
}

function AskedChange({ request }: RequestSlotProps) {
  const diff = editDiff(request.tool, request.input);
  if (!diff) return null;
  return (
    <>
      <div className="muted">{str(request.input.file_path ?? request.input.notebook_path ?? "")}</div>
      <DiffView diff={diff} />
    </>
  );
}

/** A `Read`: one line, its text once opened. */
function Read({ call, open }: ToolSlotProps) {
  return open && call.result !== undefined ? <ToolOutput text={call.result} error={call.status === "error"} open /> : null;
}

export function activate(ctx: PluginContext): void {
  for (const tool of EDITS) {
    ctx.slots.add(toolSlot(tool), { mount: "single" }, Change);
    ctx.slots.add(requestSlot(tool), {}, AskedChange);
  }
  ctx.slots.add(toolSlot("Read"), { mount: "single" }, Read);
}
