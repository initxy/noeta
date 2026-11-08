// The findings plugin's web part: a `ReportFindings` call's card lists the
// review's findings (the call's input, which the plugin also hands on as a
// notice of kind `findings`), most severe first, as the model ordered them.
import { ToolOutput } from "@initxy/noeta-ui";
import { toolSlot, type PluginContext, type ToolSlotProps } from "@initxy/noeta-web-sdk";

const TOOL = "ReportFindings";

export interface Finding {
  file: string;
  line?: number;
  summary: string;
  short_summary?: string;
  category?: string;
  verdict?: string;
  outcome?: string;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function findingsOf(input: Record<string, unknown>): Finding[] {
  const list = Array.isArray(input.findings) ? input.findings : [];
  return list
    .filter((f): f is Record<string, unknown> => typeof f === "object" && f !== null)
    .map((f) => ({
      file: str(f.file),
      line: typeof f.line === "number" ? f.line : undefined,
      summary: str(f.summary),
      short_summary: str(f.short_summary) || undefined,
      category: str(f.category) || undefined,
      verdict: str(f.verdict) || undefined,
      outcome: str(f.outcome) || undefined,
    }));
}

function Findings({ call }: ToolSlotProps) {
  const findings = findingsOf(call.input);
  const level = str(call.input.level);
  if (call.status === "error") return call.result !== undefined ? <ToolOutput text={call.result} error /> : null;
  return (
    <div className="findings">
      <div className="muted">
        Code review{level ? ` (${level})` : ""}: {findings.length} finding{findings.length === 1 ? "" : "s"}
      </div>
      {findings.length > 0 && (
        <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
          {findings.map((f, i) => (
            <li key={i} title={f.summary}>
              <code>{f.line !== undefined ? `${f.file}:${f.line}` : f.file}</code> — {f.short_summary ?? f.summary}
              {[f.category, f.verdict?.toLowerCase(), f.outcome?.replace(/_/g, " ")].filter(Boolean).map((t) => (
                <span key={t} className="muted">
                  {" "}
                  · {t}
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function activate(ctx: PluginContext): void {
  ctx.slots.add(toolSlot(TOOL), { mount: "single" }, Findings);
}
