import { useState, type ReactNode } from "react";
import type { Item } from "../state/transcript";
import { useApi, useTask } from "../state/useTask";
import { DiffView, EDITS, editDiff } from "./DiffView";
import { Markdown } from "./Markdown";
import { Transcript } from "./Transcript";

type Tool = Extract<Item, { kind: "tool" }>;

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/** The call's main argument, as Claude Code's tool lines name it. */
export function toolTarget(name: string, input: Record<string, unknown>): string {
  const v = input.file_path ?? input.path ?? input.command ?? input.pattern ?? input.url ?? input.query ?? input.description ?? input.subject ?? "";
  const first = str(v).split("\n")[0];
  return name === "Bash" ? first : first.replace(/^.*\/(?=[^/]+\/[^/]+$)/, "…/");
}

const COMPACT = new Set(["Read", "Grep", "Glob", "LS", "ToolSearch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite", "NotebookRead"]);

function StatusDot({ status }: { status: Tool["status"] }) {
  return <span className={`dot dot-${status}`} aria-label={status} />;
}

function Output({ text, error, open }: { text: string; error?: boolean; open?: boolean }) {
  const lines = text.split("\n");
  const long = lines.length > 12 || text.length > 1500;
  const [expanded, setExpanded] = useState(open ?? !long);
  if (!text) return null;
  return (
    <div className="tool-output">
      <pre className={error ? "error" : ""}>{expanded ? text : lines.slice(0, 6).join("\n").slice(0, 600)}</pre>
      {long && (
        <button className="link" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Show less" : `Show all ${lines.length} lines`}
        </button>
      )}
    </div>
  );
}

function SubAgent({ t }: { t: Tool }) {
  const api = useApi();
  const [open, setOpen] = useState(false);
  return (
    <div className="subagent">
      <button className="link" onClick={() => setOpen(!open)} disabled={!t.child}>
        {open ? "Hide" : "Show"} the sub-agent's work{t.childStatus ? ` (${t.childStatus})` : ""}
      </button>
      {open && t.child && <ChildTranscript api={api} taskId={t.child} />}
    </div>
  );
}

function ChildTranscript({ api, taskId }: { api: ReturnType<typeof useApi>; taskId: string }) {
  const [state] = useTask(api, taskId);
  return (
    <div className="nested">
      <Transcript state={state} nested />
    </div>
  );
}

/** One tool call: its name and target, its state, and the view its tool
 * deserves — a shell command and its output, a diff, a sub-agent's work. */
export function ToolCard({ t }: { t: Tool }) {
  const [open, setOpen] = useState(false);
  const mcp = t.name.startsWith("mcp__");
  const label = mcp ? t.name.slice(5).split("__").join(" · ") : t.name;
  const target = toolTarget(t.name, t.input);
  let body: ReactNode = null;
  if (t.name === "Bash") {
    body = (
      <>
        <pre className="command">$ {str(t.input.command)}</pre>
        {t.progress && t.status === "running" && <Output text={t.progress} />}
        {t.result !== undefined && <Output text={t.result} error={t.status === "error"} />}
      </>
    );
  } else if (EDITS.has(t.name)) {
    const done = t.meta?.diff;
    const diff = typeof done === "string" && done ? done : editDiff(t.name, t.input);
    const added = t.meta?.added as number | undefined;
    const removed = t.meta?.removed as number | undefined;
    body = (
      <>
        {added !== undefined && (
          <div className="diffstat">
            <span className="add">+{added}</span> <span className="del">-{removed ?? 0}</span>
          </div>
        )}
        {diff && <DiffView diff={diff} />}
        {t.status === "error" && t.result && <Output text={t.result} error />}
      </>
    );
  } else if (t.name === "Agent" || t.name === "Task") {
    body = (
      <>
        <div className="muted">
          {str(t.input.subagent_type) || "general-purpose"} · {str(t.input.description)}
        </div>
        <SubAgent t={t} />
        {t.result !== undefined && t.status === "error" && <Output text={t.result} error />}
      </>
    );
  } else if (t.name === "ExitPlanMode") {
    // While it waits, the plan is on the review card.
    body = t.status === "done" || t.status === "error" ? <Markdown text={str(t.input.plan)} /> : null;
  } else if (COMPACT.has(t.name)) {
    body = open && t.result !== undefined ? <Output text={t.result} error={t.status === "error"} open /> : null;
  } else {
    const input = t.name === "WebFetch" ? str(t.input.prompt) : JSON.stringify(t.input, null, 2);
    body = (
      <>
        {open && <pre className="input">{input}</pre>}
        {t.result !== undefined && <Output text={t.result} error={t.status === "error"} />}
      </>
    );
  }
  const summary = COMPACT.has(t.name) && t.result !== undefined && !open ? `${t.result.split("\n").length} lines` : "";
  return (
    <div className={`tool tool-${COMPACT.has(t.name) ? "compact" : "full"}`} data-tool={t.name} data-status={t.status}>
      <div className="tool-head" onClick={() => setOpen(!open)}>
        <StatusDot status={t.status} />
        <span className="tool-name">{label}</span>
        <span className="tool-target">{target}</span>
        {summary && <span className="muted"> · {summary}</span>}
        {t.status === "waiting" && <span className="badge">waiting for you</span>}
      </div>
      {body && <div className="tool-body">{body}</div>}
    </div>
  );
}
