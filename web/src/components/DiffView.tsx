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
