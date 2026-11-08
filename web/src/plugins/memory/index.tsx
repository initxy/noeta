// The memory plugin's web part: where the session keeps its memory (the
// plugin's `dir` record), in the plugins dialog's Settings tab.
import { Panel } from "@initxy/noeta-ui";
import { SLOTS, useRecords, type PluginContext } from "@initxy/noeta-web-sdk";

/** The memory directory last recorded, "" before one is. */
export function memoryDir(records: unknown[]): string {
  let dir = "";
  for (const r of records) {
    const d = (r as { dir?: unknown } | null)?.dir;
    if (typeof d === "string") dir = d;
  }
  return dir;
}

export function activate(ctx: PluginContext): void {
  const Section = () => {
    const dir = memoryDir(useRecords(ctx, "dir"));
    return (
      <Panel title="Memory">
        {dir ? (
          <div>
            This session's memory is in <code>{dir}</code>.
          </div>
        ) : (
          <div className="muted">This session keeps no memory directory yet.</div>
        )}
      </Panel>
    );
  };
  ctx.slots.add(SLOTS.settings, { key: "memory", mount: "keyed" }, Section);
}
