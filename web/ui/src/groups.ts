// How a transcript folds runs of tool calls, Claude Code's way and the
// TUI's (tui/src/noeta_tui/render.py): pure, over a front end's items. It
// names Claude Code's tools, as Claude Code's own rules do; the page's own
// files name none (web/src/plugins/boundary.test.ts).
//
// - Quiet calls one after another (reads, searches, listings, fetches, MCP
//   and other tools, shell commands) are one line from the first of them:
//   "Searched for 2 patterns, read 3 files, ran 1 shell command", opened on
//   a click. Short text between them (1–2 lines, no code or list) and the
//   model's thoughts stay inside; the latest such text shows dimmed under
//   the line, and while a call runs, its command or pattern.
// - A failed call, or one waiting for an approval, is still counted but
//   drawn on its own under the line.
// - A shell command that only reads (cat, grep, ls, git log …) counts as a
//   read, a search or a listing; git commit / push and gh pr are named
//   ("committed 1a2b3c4", "pushed to main", "created PR #12").
// - Bookkeeping calls (the task list, ToolSearch) are not drawn and do not
//   split a run; the task panel shows the list.
// - Edits of one file one after another are one line ("Edited calc.py ×3
//   (+12 −4)") with the latest diff; agents started one after another, one
//   line with a line per agent.
// - Anything else (the person's message, longer text, an edit, an agent, a
//   question, a notice) ends the run.
/** A tool call as the transcript holds it. */
export interface ToolCallItem {
  kind: "tool";
  id: string;
  name: string;
  input: Record<string, unknown>;
  status: "pending" | "running" | "waiting" | "done" | "error";
  result?: string;
  meta?: Record<string, unknown>;
  children?: { id: string; status?: string }[];
}

/** An item of a transcript: a tool call, or anything else (text: the model's, for "assistant"). */
export type GroupItem = ToolCallItem | { kind: "user" | "assistant" | "thinking" | "notice" | "compaction"; id: string; text?: string };

type Tool = ToolCallItem;
type Item = GroupItem;

/** `from`, `to`: the items it covers (`to` past the last). */
export type Block<I extends GroupItem = GroupItem> =
  | { kind: "item"; id: string; item: I; from: number; to: number }
  /** A run of quiet calls; `items` are its calls with the text and thoughts between them. */
  | { kind: "group"; id: string; items: I[]; from: number; to: number }
  | { kind: "edits"; id: string; path: string; calls: Extract<I, { kind: "tool" }>[]; from: number; to: number }
  | { kind: "agents"; id: string; calls: Extract<I, { kind: "tool" }>[]; from: number; to: number }
  /** Items not drawn (bookkeeping calls); their place keeps the records shown after them. */
  | { kind: "skip"; id: string; from: number; to: number };

export type CallKind = "hidden" | "own" | "git" | "search" | "read" | "list" | "fetch" | "web" | "mcp" | "tool" | "shell";

const HIDDEN = new Set(["TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "ToolSearch"]);
const EDITS = new Set(["Edit", "MultiEdit", "Write", "NotebookEdit"]);
const AGENTS = new Set(["Agent", "Task"]);
const OWN = new Set([...EDITS, ...AGENTS, "Workflow", "AskUserQuestion", "ExitPlanMode", "EnterPlanMode", "Skill"]);
const KINDS: Record<string, CallKind> = { Read: "read", Grep: "search", Glob: "search", LS: "list", WebFetch: "fetch", WebSearch: "web" };

const str = (v: unknown): string => (typeof v === "string" ? v : "");

// Shell commands that only look: the first word of every part of a command
// line (`a | b && c`), past `cd …` and VAR=value.
const READS = new Set(["cat", "head", "tail", "less", "more", "wc", "nl", "bat", "stat", "file", "md5sum", "sha256sum"]);
const SEARCHES = new Set(["grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate", "which", "whereis"]);
const LISTS = new Set(["ls", "tree", "du", "pwd"]);
/** What may follow a looking command in a pipe or a list without changing anything. */
const FILTERS = new Set(["sort", "uniq", "cut", "tr", "jq", "echo", "printf", "true", "column", "head", "tail", "wc", "grep", "rg", "cd"]);
const GIT_LOOKS = /^git\s+(?:-C\s+\S+\s+)?(status|log|diff|show|blame|branch|rev-parse|ls-files|grep)\b/;

/** What a shell command counts as: a look (read, search, list), a git
 * commit / push / PR (git), else a shell command. */
export function shellKind(command: string): "read" | "search" | "list" | "git" | "shell" {
  const line = command.trim();
  if (/\bgit\s+(?:-C\s+\S+\s+)?(commit|push)\b|\bgh\s+pr\s+(create|merge)\b/.test(line)) return "git";
  // Writing anywhere but /dev/null, or running something in the background, is not a look.
  const quiet = line.replace(/\d?>&\d|\d?>\s*\/dev\/null/g, "");
  if (/[>`]|\$\(|(^|[^&])&($|[^&])/.test(quiet) || line.includes("\n")) return "shell";
  let first: "read" | "search" | "list" | null = null;
  for (const part of quiet.split(/\|\||&&|[|;]/)) {
    const words = part.trim().split(/\s+/).filter((w) => !/^\w+=/.test(w));
    const head = words[0] ?? "";
    if (!head) continue;
    const joined = words.join(" ");
    const kind = READS.has(head) || GIT_LOOKS.test(joined) ? "read" : SEARCHES.has(head) || joined.startsWith("git grep") ? "search" : LISTS.has(head) ? "list" : null;
    if (head === "cd" && first === null) continue;
    if (first === null) {
      if (!kind) return "shell";
      if (head === "find" && /\s-(delete|exec|execdir|ok)\b/.test(joined)) return "shell";
      first = kind;
    } else if (!kind && !FILTERS.has(head)) return "shell";
  }
  return first ?? "shell";
}

export function callKind(t: Tool): CallKind {
  if (HIDDEN.has(t.name)) return "hidden";
  if (OWN.has(t.name)) return "own";
  if (t.name.startsWith("mcp__")) return "mcp";
  if (t.name === "Bash" || t.name === "PowerShell") return shellKind(str(t.input.command));
  return KINDS[t.name] ?? "tool";
}

/** Text short enough to sit inside a run: one or two lines, no code, list, heading or table. */
export function shortText(text: string): boolean {
  const t = text.trim();
  return t.length <= 240 && t.split("\n").length <= 2 && !/```|~~~|^\s*([-*+]|\d+[.)])\s|^\s*#|^\s*\|/m.test(t);
}

const groupable = (k: CallKind) => k !== "hidden" && k !== "own";

/** The items as the transcript draws them: runs of quiet calls folded, edits
 * of one file and agents started together merged. */
export function groupItems<I extends GroupItem>(items: I[]): Block<I>[] {
  type T = Extract<I, { kind: "tool" }>;
  const out: Block<I>[] = [];
  let group: Extract<Block<I>, { kind: "group" }> | null = null;
  // Items after the run's last call that join it only if another call follows.
  let between: number[] = [];
  const alone = (i: number): Block<I> => {
    const it = items[i];
    return it.kind === "tool" && callKind(it) === "hidden" && it.status !== "error"
      ? { kind: "skip", id: `s:${it.id}`, from: i, to: i + 1 }
      : { kind: "item", id: it.id, item: it, from: i, to: i + 1 };
  };
  const close = () => {
    if (group) out.push(group);
    for (const i of between) out.push(alone(i));
    group = null;
    between = [];
  };
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const kind = it.kind === "tool" ? callKind(it) : null;
    if (kind !== null && groupable(kind)) {
      if (group) {
        for (const j of between) group.items.push(items[j]);
        group.items.push(it);
        group.to = i + 1;
      } else {
        group = { kind: "group", id: `g:${it.id}`, items: [it], from: i, to: i + 1 };
      }
      between = [];
      continue;
    }
    const quiet = (kind === "hidden" && (it as Tool).status !== "error") || it.kind === "thinking" || (it.kind === "assistant" && shortText(it.text ?? ""));
    if (group && quiet) {
      between.push(i);
      continue;
    }
    close();
    if (it.kind === "tool" && EDITS.has(it.name) && mergeable(it)) {
      const path = str(it.input.file_path ?? it.input.notebook_path);
      let j = i + 1;
      while (j < items.length) {
        const next = items[j];
        if (next.kind !== "tool" || !EDITS.has(next.name) || !mergeable(next) || str(next.input.file_path ?? next.input.notebook_path) !== path) break;
        j++;
      }
      if (j - i > 1 && path) {
        out.push({ kind: "edits", id: `e:${it.id}`, path, calls: items.slice(i, j) as T[], from: i, to: j });
        i = j - 1;
        continue;
      }
    }
    if (it.kind === "tool" && AGENTS.has(it.name)) {
      let j = i + 1;
      while (j < items.length && items[j].kind === "tool" && AGENTS.has((items[j] as Tool).name)) j++;
      if (j - i > 1) {
        out.push({ kind: "agents", id: `a:${it.id}`, calls: items.slice(i, j) as T[], from: i, to: j });
        i = j - 1;
        continue;
      }
    }
    out.push(alone(i));
  }
  close();
  return out;
}

/** An edit merged with its neighbours: not one that failed or waits for the person. */
const mergeable = (t: Tool) => t.status !== "error" && t.status !== "waiting";

const running = (t: Tool) => t.status === "pending" || t.status === "running";

/** A run's calls drawn on their own under its line: failed, or waiting for the person. */
export const poppedOut = (t: Tool) => t.status === "error" || t.status === "waiting";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function readPaths(t: Tool): string[] {
  if (t.name === "Read") return [str(t.input.file_path)].filter(Boolean);
  const command = str(t.input.command).split(/\||&&|;/)[0].trim().split(/\s+/);
  return command.slice(1).filter((w) => w && !w.startsWith("-") && !/^\d+$/.test(w));
}

function commitOf(t: Tool): string {
  return /\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,})\]/.exec(t.result ?? "")?.[1] ?? "";
}

function pushTarget(command: string): string {
  const m = /\bgit\s+(?:-C\s+\S+\s+)?push\b([^|;&]*)/.exec(command);
  const args = (m?.[1] ?? "").trim().split(/\s+/).filter((w) => w && !w.startsWith("-"));
  return (args[1] ?? args[0] ?? "").replace(/^HEAD:/, "") || "origin";
}

/** A run's line: Claude Code's words, what is running in the present tense
 * and ending in "…"; the latest command or pattern while one runs; the
 * latest text between its calls. */
export function groupSummary(items: Item[]): { line: string; active: boolean; hint: string; text: string; failed: number } {
  const calls = items.filter((i): i is Tool => i.kind === "tool");
  const active = calls.some(running);
  const reads = new Set<string>();
  let readOps = 0;
  const count: Record<string, number> = {};
  const servers: string[] = [];
  const commits: string[] = [];
  const pushes: string[] = [];
  const prs: string[] = [];
  for (const t of calls) {
    const kind = callKind(t);
    if (kind === "read") {
      const paths = readPaths(t);
      if (paths.length) for (const p of paths) reads.add(p);
      else readOps++;
    } else if (kind === "mcp") {
      count.mcp = (count.mcp ?? 0) + 1;
      const server = t.name.slice(5).split("__")[0];
      if (!servers.includes(server)) servers.push(server);
    } else if (kind === "git") {
      const command = str(t.input.command);
      if (/\bgit\s+(?:-C\s+\S+\s+)?commit\b/.test(command)) commits.push(commitOf(t));
      if (/\bgit\s+(?:-C\s+\S+\s+)?push\b/.test(command)) pushes.push(pushTarget(command));
      if (/\bgh\s+pr\s+(create|merge)\b/.test(command)) prs.push(/\/pull\/(\d+)/.exec(t.result ?? "")?.[1] ?? "");
    } else count[kind] = (count[kind] ?? 0) + 1;
  }
  const parts: string[] = [];
  const say = (now: string, done: string, what: string) => parts.push(`${active ? now : done} ${what}`.trim());
  if (commits.length) {
    const shas = commits.filter(Boolean);
    say("committing", "committed", shas.length ? shas.join(", ") : commits.length > 1 ? `${commits.length} times` : "");
  }
  if (pushes.length) say("pushing to", "pushed to", [...new Set(pushes)].join(", "));
  if (prs.length) say("creating", "created", prs.map((n) => (n ? `PR #${n}` : "a PR")).join(", "));
  if (count.search) say("searching for", "searched for", plural(count.search, "pattern", "patterns"));
  const read = reads.size + readOps;
  if (read) say("reading", "read", plural(read, "file", "files"));
  if (count.list) say("listing", "listed", plural(count.list, "directory", "directories"));
  if (count.fetch) say("fetching", "fetched", plural(count.fetch, "page", "pages"));
  if (count.web) say("running", "ran", plural(count.web, "web search", "web searches"));
  if (count.mcp) say("calling", "called", `${servers.join(", ")}${count.mcp > 1 ? ` ${count.mcp} times` : ""}`);
  if (count.tool) say("calling", "called", plural(count.tool, "tool", "tools"));
  if (count.shell) say("running", "ran", plural(count.shell, "shell command", "shell commands"));
  const joined = parts.join(", ");
  const line = (joined.charAt(0).toUpperCase() + joined.slice(1) || "Called 0 tools") + (active ? "…" : "");
  const latest = [...calls].reverse().find(running);
  const hint = latest ? callTarget(latest) : "";
  const lastText = [...items].reverse().find((i) => i.kind === "assistant");
  return { line, active, hint, text: lastText?.kind === "assistant" ? (lastText.text ?? "").trim() : "", failed: calls.filter((t) => t.status === "error").length };
}

/** A call's command, pattern, address or path, on one line. */
export function callTarget(t: Tool): string {
  const i = t.input;
  return str(i.command ?? i.pattern ?? i.url ?? i.query ?? i.file_path ?? i.path ?? i.description).split("\n")[0];
}

const basename = (path: string) => path.replace(/\/+$/, "").split("/").pop() ?? path;

/** Edits of one file as one line: "Edited calc.py ×3 (+12 −4)". */
export function editsSummary(path: string, calls: Tool[]): string {
  const active = calls.some(running);
  let added = 0;
  let removed = 0;
  let known = false;
  for (const t of calls) {
    if (typeof t.meta?.added === "number") {
      added += t.meta.added;
      removed += typeof t.meta.removed === "number" ? t.meta.removed : 0;
      known = true;
    }
  }
  return `${active ? "Editing" : "Edited"} ${basename(path)} ×${calls.length}${known ? ` (+${added} −${removed})` : ""}`;
}

/** Agents started together as one line: "Running 3 agents", "Ran 3 agents". */
export function agentsSummary(calls: Tool[]): string {
  const active = calls.some((t) => running(t) || t.children?.some((c) => !c.status));
  return `${active ? "Running" : "Ran"} ${plural(calls.length, "agent", "agents")}`;
}
