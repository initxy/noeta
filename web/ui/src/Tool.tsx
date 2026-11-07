import { useState } from "react";

/** A tool's output: long output folded to its first lines until asked for. */
export function ToolOutput({ text, error, open }: { text: string; error?: boolean; open?: boolean }) {
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

/** A unified diff, line by line, added and removed lines marked. */
export function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n").filter((l) => !l.startsWith("+++") && !l.startsWith("---"));
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return (
    <pre className="diff" aria-label="diff">
      {lines.map((line, i) => {
        const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
        return (
          <div key={i} className={`diff-${kind}`}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

/** A diff for a change that has not happened yet: `before` replaced by `after`. */
export function proposedDiff(before: string, after: string): string {
  const old = before ? before.split("\n").map((l) => `-${l}`) : [];
  const neu = after ? after.split("\n").map((l) => `+${l}`) : [];
  return [...old, ...neu].join("\n");
}
