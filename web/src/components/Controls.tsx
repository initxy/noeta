import { useEffect, useState } from "react";
import type { Api } from "../api/client";
import type { ModelInfo, RewindPoint, TreeUsage } from "../api/types";
import { contextLeft, type AutoCompact, type Modes } from "../state/transcript";

/** Claude Code's modes when the task has not published its own (a new session). */
export const DEFAULT_MODES: Modes = {
  modes: ["default", "acceptEdits", "plan", "auto"],
  cycle: ["default", "acceptEdits", "plan", "auto"],
  names: {
    default: "Ask before edits",
    acceptEdits: "Accept edits",
    plan: "Plan mode",
    auto: "Auto",
    bypassPermissions: "Bypass permissions",
  },
};

const SHORT: Record<string, string> = {
  default: "Ask before edits",
  acceptEdits: "Accept edits",
  plan: "Plan mode",
  auto: "Auto",
  dontAsk: "Don't ask",
  bypassPermissions: "Bypass permissions",
};

export function ModePicker({ mode, modes, onChange }: { mode: string; modes: Modes | null; onChange: (m: string) => void }) {
  const offered = (modes ?? DEFAULT_MODES).cycle;
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

export function ModelPicker({ model, models, onChange }: { model: string; models: ModelInfo[]; onChange: (m: string) => void }) {
  const ids = models.map((m) => m.id);
  return (
    <select className="picker" aria-label="Model" value={model} onChange={(e) => onChange(e.target.value)}>
      {!ids.includes(model) && <option value={model}>{model || "server's model"}</option>}
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.aliases[0] ? `${m.aliases[0]} (${m.id})` : m.id}
        </option>
      ))}
    </select>
  );
}

const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));

/** Tokens and cost of the session and its sub-agents, and how full the context is. */
/** Tokens and cost; then the context's share of the window, or (near the
 * auto-compact threshold, as Claude Code's footer does) how far it is left. */
export function UsageBadge({
  usage,
  contextTokens,
  window,
  compaction = null,
}: {
  usage: TreeUsage | null;
  contextTokens: number;
  window: number;
  compaction?: AutoCompact | null;
}) {
  const left = contextLeft(contextTokens, compaction);
  if (!usage) return left ? <span className={left.low ? "usage context-low" : "usage"}>{left.text}</span> : null;
  const u = usage.usage;
  const total = (u.uncached ?? 0) + (u.cache_read ?? 0) + (u.cache_write ?? 0) + (u.output ?? 0);
  const pct = window && contextTokens ? Math.round((contextTokens / window) * 100) : 0;
  return (
    <span className="usage" title={Object.entries(usage.by_model).map(([m, v]) => `${m}: ${v.calls} calls, $${v.cost_usd.toFixed(4)}`).join("\n")}>
      {fmt(total)} tokens · ${usage.cost_usd.toFixed(2)}
      {left ? (
        <>
          {" · "}
          <span className={left.low ? "context-low" : undefined}>{left.text}</span>
        </>
      ) : (
        pct > 0 && ` · ${pct}% context`
      )}
    </span>
  );
}

/** Go back to before one of the person's prompts: the conversation, the files, or both. */
export function RewindDialog({ api, taskId, onClose, onRewound }: { api: Api; taskId: string; onClose: () => void; onRewound: (taskId: string, prompt: string) => void }) {
  const [points, setPoints] = useState<RewindPoint[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.rewindPoints(taskId).then(setPoints, (e: unknown) => setError(String(e)));
  }, [api, taskId]);
  const go = async (p: RewindPoint, conversation: boolean, files: boolean) => {
    try {
      const done = await api.rewind(taskId, p.index, conversation, files);
      onRewound(done.task_id, done.prompt);
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog" role="dialog" aria-label="Rewind" onClick={(e) => e.stopPropagation()}>
        <div className="card-title">Rewind to before a prompt</div>
        {error && <div className="notice error">{error}</div>}
        {!points && !error && <div className="muted">Loading…</div>}
        {points?.length === 0 && <div className="muted">Nothing to rewind to yet.</div>}
        <ul className="rewind-list">
          {points
            ?.slice()
            .reverse()
            .map((p) => (
              <li key={p.index}>
                <div className="rewind-text">{p.text}</div>
                <div className="muted">{p.files.length ? `${p.files.length} file(s) changed since` : "no file changes since"}</div>
                <div className="choices inline">
                  <button className="choice" onClick={() => go(p, true, true)}>
                    Both
                  </button>
                  <button className="choice" onClick={() => go(p, true, false)}>
                    Conversation
                  </button>
                  <button className="choice" disabled={!p.files.length} onClick={() => go(p, false, true)}>
                    Code
                  </button>
                </div>
              </li>
            ))}
        </ul>
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
