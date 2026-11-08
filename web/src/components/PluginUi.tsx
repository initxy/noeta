// What a task's plugins show through their UI data (`GET /tasks/{id}/ui`):
// status items, widgets above or below the composer, notices.
import { Markdown } from "@initxy/noeta-ui";
import { Slot, SLOTS, type StatusbarSlotProps } from "@initxy/noeta-web-sdk";
import { useState, type ReactNode } from "react";
import type { UiEntry } from "../api/types";

type Of<T extends UiEntry["type"]> = Extract<UiEntry, { type: T }>;

/** The line under the composer, the `statusbar` slot: its own content is
 * `children` (the model picker), then each plugin's status items; plugins'
 * contributions come after them, or with a negative `order` before all. */
export function StatusBar({ items, context, children }: { items: UiEntry[]; context: StatusbarSlotProps["context"]; children?: ReactNode }) {
  const status = items.filter((i): i is Of<"status"> => i.type === "status");
  return (
    <Slot name={SLOTS.statusbar} props={{ context }}>
      {children}
      <div className="statusbar" aria-label="Status">
        {status.map((s) => (
          <span key={`${s.plugin}/${s.key}`} className={`status-item tone-${s.tone}`} title={s.plugin}>
            {s.text}
          </span>
        ))}
      </div>
    </Slot>
  );
}

/** The widgets placed `above` or `below` the composer. */
export function Widgets({ items, placement }: { items: UiEntry[]; placement: "above" | "below" }) {
  const shown = items.filter((i): i is Of<"widget"> => i.type === "widget" && i.placement === placement);
  if (!shown.length) return null;
  return (
    <div className={`widgets widgets-${placement}`}>
      {shown.map((w) => (
        <div key={`${w.plugin}/${w.key}`} className="widget" data-plugin={w.plugin}>
          <Markdown text={w.markdown} />
        </div>
      ))}
    </div>
  );
}

/** The plugins' notices; one closed stays closed until its text changes. */
export function Notices({ items }: { items: UiEntry[] }) {
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const shown = items.filter((i): i is Of<"notice"> => i.type === "notice" && !closed.has(`${i.plugin}/${i.key}/${i.text}`));
  if (!shown.length) return null;
  return (
    <div className="plugin-notices">
      {shown.map((n) => (
        <div key={`${n.plugin}/${n.key}`} className={`notice ${n.level} plugin-notice`} role="status">
          <span>
            {n.plugin}: {n.text}
          </span>
          <button className="icon-button" aria-label="Dismiss" onClick={() => setClosed(new Set(closed).add(`${n.plugin}/${n.key}/${n.text}`))}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
