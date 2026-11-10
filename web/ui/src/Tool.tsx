import { useState } from "react";

/** A tool's error without the `<tool_use_error>` tags the model reads it in. */
export function toolError(text: string): string {
  return text.replace(/^\s*<tool_use_error>([\s\S]*?)<\/tool_use_error>\s*$/, "$1").trim();
}

/** A tool's output: long output folded to its first lines until asked for;
 * a one-line error as one red line. */
export function ToolOutput({ text: given, error, open }: { text: string; error?: boolean; open?: boolean }) {
  const text = error ? toolError(given) : given;
  const lines = text.split("\n");
  const long = lines.length > 12 || text.length > 1500;
  const [expanded, setExpanded] = useState(open ?? !long);
  if (!text) return null;
  if (error && lines.length === 1) return <div className="tool-error">{text}</div>;
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

interface DiffLine {
  kind: "hunk" | "add" | "del" | "ctx";
  text: string;
  /** The line's number in the file before and after, when the diff has hunk headers to count from. */
  old?: number;
  new?: number;
}

/** A unified diff's lines, numbered from its hunk headers (`@@ -a,b +c,d @@`). */
export function diffLines(diff: string): DiffLine[] {
  const lines = diff.split("\n").filter((l) => !l.startsWith("+++") && !l.startsWith("---"));
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  let old: number | undefined;
  let neu: number | undefined;
  return lines.map((text): DiffLine => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(text);
    if (hunk) {
      old = Number(hunk[1]);
      neu = Number(hunk[2]);
      return { kind: "hunk", text };
    }
    if (text.startsWith("+")) return { kind: "add", text, new: neu === undefined ? undefined : neu++ };
    if (text.startsWith("-")) return { kind: "del", text, old: old === undefined ? undefined : old++ };
    return { kind: "ctx", text, old: old === undefined ? undefined : old++, new: neu === undefined ? undefined : neu++ };
  });
}

/** Lines a diff shows before "Show N more lines". */
const DIFF_FOLD = 12;

/** A unified diff, line by line: added and removed lines marked, numbered
 * when it has hunk headers, a long one folded to its first lines. */
export function DiffView({ diff, open }: { diff: string; open?: boolean }) {
  const rows = diffLines(diff);
  const numbered = rows.some((r) => r.old !== undefined || r.new !== undefined);
  // Folding away only a line or two would cost as much as showing them.
  const long = rows.length > DIFF_FOLD + 3;
  const [expanded, setExpanded] = useState(open ?? false);
  const shown = long && !expanded ? rows.slice(0, DIFF_FOLD) : rows;
  return (
    <div className="diff-view">
      <pre className={numbered ? "diff numbered" : "diff"} aria-label="diff">
        {shown.map((r, i) => (
          <div key={i} className={`diff-${r.kind}`}>
            {numbered && r.kind !== "hunk" && (
              <>
                <span className="ln" aria-hidden>
                  {r.old ?? ""}
                </span>
                <span className="ln" aria-hidden>
                  {r.new ?? ""}
                </span>
              </>
            )}
            <span className="diff-text">{r.text || " "}</span>
          </div>
        ))}
      </pre>
      {long && (
        <button className="link" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Show less" : `Show ${rows.length - DIFF_FOLD} more lines`}
        </button>
      )}
    </div>
  );
}

/** A diff for a change that has not happened yet: `before` replaced by `after`. */
export function proposedDiff(before: string, after: string): string {
  const old = before ? before.split("\n").map((l) => `-${l}`) : [];
  const neu = after ? after.split("\n").map((l) => `+${l}`) : [];
  return [...old, ...neu].join("\n");
}
