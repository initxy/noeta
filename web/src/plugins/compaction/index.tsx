// The compaction plugin's web part: near the point it compacts at (its
// `threshold` record), the status bar counts down to it, as Claude Code's
// footer does.
import { SLOTS, useRecords, type PluginContext, type StatusbarSlotProps } from "@initxy/web-sdk";

/** The plugin's `threshold` record (noeta.toolkit.plugin_records.auto_compact). */
export interface AutoCompact {
  /** Context tokens at which automatic compaction fires. */
  threshold: number;
  /** The window less the summary's reserve. */
  window: number;
  /** Whether it compacts automatically (`/compact` only when not). */
  auto: boolean;
}

/** How far under the point it counts down to the warning starts. */
export const CONTEXT_WARN_TOKENS = 20_000;

const whole = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n > 0;

/** The last well-formed `threshold` record, if any. */
export function autoCompact(records: unknown[]): AutoCompact | null {
  let found: AutoCompact | null = null;
  for (const r of records) {
    const d = (r ?? {}) as Record<string, unknown>;
    if (whole(d.threshold) && whole(d.window)) found = { threshold: d.threshold, window: d.window, auto: d.auto !== false };
  }
  return found;
}

/** Claude Code's warning once the context is within CONTEXT_WARN_TOKENS of
 * the auto-compact threshold (of the window when automatic compaction is
 * off); null before that. */
export function contextLeft(tokens: number, at: AutoCompact | null): { text: string; low: boolean } | null {
  if (!at) return null;
  const limit = at.auto ? at.threshold : at.window;
  if (limit <= 0 || tokens < limit - CONTEXT_WARN_TOKENS) return null;
  const left = Math.max(0, Math.round(((limit - tokens) / limit) * 100));
  return at.auto
    ? { text: `${left}% until auto-compact`, low: false }
    : { text: `Context low (${left}% remaining) · Run /compact to compact & continue`, low: true };
}

export function activate(ctx: PluginContext): void {
  const Countdown = ({ context }: StatusbarSlotProps) => {
    const left = contextLeft(context.tokens, autoCompact(useRecords(ctx, "threshold")));
    return left ? (
      <span className={left.low ? "context-left context-low" : "context-left"} data-plugin="compaction">
        {left.text}
      </span>
    ) : null;
  };
  ctx.slots.add(SLOTS.statusbar, { key: "context", mount: "keyed" }, Countdown);
}
