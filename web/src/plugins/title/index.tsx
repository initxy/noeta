// The title plugin's web part: the session's title in its header, as the
// plugin records it (`title`, from the model or a rename), live; a click
// renames it (the `rename` signal, which the plugin takes).
import { SLOTS, useRecords, type PluginContext, type SessionHeaderSlotProps } from "@noeta-ai/web-sdk";

/** The signal that renames a task (noeta.toolkit.signals.RENAME_SIGNAL). */
const RENAME_SIGNAL = "rename";

/** The title last recorded, "" before one is. */
export function titleOf(records: unknown[]): string {
  let title = "";
  for (const r of records) {
    const t = (r as { title?: unknown } | null)?.title;
    if (typeof t === "string") title = t;
  }
  return title;
}

export function activate(ctx: PluginContext): void {
  const Title = ({ title }: SessionHeaderSlotProps) => {
    const shown = titleOf(useRecords(ctx, "title")) || title;
    const rename = () => {
      const named = window.prompt("Rename the session", shown);
      if (named?.trim()) void ctx.client.signal(RENAME_SIGNAL, { title: named.trim() }).catch((e: unknown) => console.error("could not rename", e));
    };
    return (
      <button className="title" onClick={rename} title="Rename">
        {shown || "Untitled session"}
      </button>
    );
  };
  ctx.slots.add(SLOTS.sessionHeader, { mount: "single" }, Title);
}
