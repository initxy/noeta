// The permissions plugin's web part: the session's permission mode, shown
// and switched at the start of the status bar (its `mode`, `plan_exit` and
// `modes` records; the plan-mode plugin records its switches here too), and
// Shift+Tab in the composer for the next mode, as in Claude Code.
import { SLOTS, useAllRecords, type PluginContext, type PluginRecord, type WrapProps } from "@initxy/noeta-web-sdk";
import { useEffect, useState, type KeyboardEvent } from "react";

export interface Modes {
  modes: string[];
  cycle: string[];
  names: Record<string, string>;
}

/** Claude Code's modes, until the task publishes its own. */
export const DEFAULT_CYCLE = ["default", "acceptEdits", "plan", "auto"];

const SHORT: Record<string, string> = {
  default: "Ask before edits",
  acceptEdits: "Accept edits",
  plan: "Plan mode",
  auto: "Auto",
  dontAsk: "Don't ask",
  bypassPermissions: "Bypass permissions",
};

/** The signal that switches the mode (noeta.toolkit.permission_modes.MODE_SIGNAL). */
const MODE_SIGNAL = "permission_mode";

/** The mode last recorded and the modes last offered. */
export function modeOf(records: PluginRecord[]): { mode: string | null; modes: Modes | null } {
  let mode: string | null = null;
  let modes: Modes | null = null;
  for (const { kind, data } of records) {
    const d = (data ?? {}) as Record<string, unknown>;
    if ((kind === "mode" || kind === "plan_exit") && typeof d.mode === "string" && d.mode) mode = d.mode;
    else if (kind === "modes" && Array.isArray(d.cycle)) modes = d as unknown as Modes;
  }
  return { mode, modes };
}

export function ModePicker({ mode, modes, onChange }: { mode: string; modes: Modes | null; onChange: (m: string) => void }) {
  const offered = modes?.cycle ?? DEFAULT_CYCLE;
  const list = offered.includes(mode) || !mode ? offered : [mode, ...offered];
  return (
    <select className={`picker mode-${mode}`} aria-label="Permission mode" value={mode || "default"} onChange={(e) => onChange(e.target.value)}>
      {list.map((m) => (
        <option key={m} value={m} title={modes?.names[m]}>
          {SHORT[m] ?? m}
        </option>
      ))}
    </select>
  );
}

export function activate(ctx: PluginContext): void {
  // Before the plugin records the mode, the one the task was made with.
  let started: string | null = null;
  const startedWith = ctx.client.read(0).then(
    (log) => {
      const config = log[0]?.event.type === "TaskCreated" ? (log[0].event.config as Record<string, unknown> | undefined) : undefined;
      started = typeof config?.permission_mode === "string" ? config.permission_mode : null;
      return started;
    },
    () => null,
  );
  const current = () => {
    const { mode, modes } = modeOf(ctx.allRecords());
    return { mode: mode ?? started ?? "default", modes };
  };
  const change = (mode: string) => void ctx.client.signal(MODE_SIGNAL, { mode }).catch((e: unknown) => console.error("could not switch the mode", e));

  const Picker = () => {
    useAllRecords(ctx);
    const [, setStarted] = useState(started);
    useEffect(() => void startedWith.then(setStarted), []);
    const { mode, modes } = current();
    return <ModePicker mode={mode} modes={modes} onChange={change} />;
  };
  ctx.slots.add(SLOTS.statusbar, { key: "mode", mount: "keyed", order: -10 }, Picker);

  const ShiftTab = ({ children }: WrapProps) => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab" || !e.shiftKey || !(e.target instanceof HTMLTextAreaElement)) return;
      e.preventDefault();
      e.stopPropagation();
      const { mode, modes } = current();
      const cycle = modes?.cycle ?? DEFAULT_CYCLE;
      change(cycle[(cycle.indexOf(mode) + 1) % cycle.length]);
    };
    return (
      <div style={{ display: "contents" }} onKeyDownCapture={onKey}>
        {children}
      </div>
    );
  };
  ctx.slots.add(SLOTS.composer, { mount: "wrap" }, ShiftTab);
}
