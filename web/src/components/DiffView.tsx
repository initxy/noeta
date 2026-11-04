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

/** A diff for a change that has not run yet: `before` replaced by `after`. */
export function proposedDiff(before: string, after: string): string {
  const old = before ? before.split("\n").map((l) => `-${l}`) : [];
  const neu = after ? after.split("\n").map((l) => `+${l}`) : [];
  return [...old, ...neu].join("\n");
}

/** The tools whose call is a change to a file. */
export const EDITS = new Set(["Edit", "MultiEdit", "Write", "NotebookEdit"]);

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/** The change an edit tool's call proposes, from its input. */
export function editDiff(name: string, input: Record<string, unknown>): string {
  if (name === "Write") return proposedDiff("", str(input.content));
  if (name === "Edit") return proposedDiff(str(input.old_string), str(input.new_string));
  if (name === "NotebookEdit") return proposedDiff("", str(input.new_source));
  if (name === "MultiEdit" && Array.isArray(input.edits)) {
    return (input.edits as { old_string?: string; new_string?: string }[]).map((e) => proposedDiff(str(e.old_string), str(e.new_string))).join("\n");
  }
  return "";
}
