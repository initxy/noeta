// The hooks plugin's web part: what the session's hooks did (each record
// naming a `hook`: a block, a stop, context they added), in the plugins
// dialog's Settings tab.
import { Panel } from "@initxy/ui";
import { SLOTS, useAllRecords, type PluginContext, type PluginRecord } from "@initxy/web-sdk";

/** Hook label -> how many times it acted. */
export function hookActs(records: PluginRecord[]): [string, number][] {
  const n = new Map<string, number>();
  for (const { data } of records) {
    const hook = (data as { hook?: unknown } | null)?.hook;
    if (typeof hook === "string" && hook) n.set(hook, (n.get(hook) ?? 0) + 1);
  }
  return [...n.entries()];
}

export function activate(ctx: PluginContext): void {
  const Section = () => {
    const acts = hookActs(useAllRecords(ctx));
    return (
      <Panel title="Hooks">
        {acts.length ? (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {acts.map(([hook, times]) => (
              <li key={hook}>
                {hook}
                {times > 1 && <span className="muted"> ×{times}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">No hook has acted in this session.</div>
        )}
      </Panel>
    );
  };
  ctx.slots.add(SLOTS.settings, { key: "hooks", mount: "keyed" }, Section);
}
