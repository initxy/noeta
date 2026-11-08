// The MCP plugin's web part: the session's MCP servers, their tools and the
// ones that could not be reached (the plugin's `listed`, `server_down` and
// `server_up` records), in the plugins dialog's Settings tab.
import { Panel } from "@initxy/noeta-ui";
import { SLOTS, useAllRecords, type PluginContext, type PluginRecord } from "@initxy/noeta-web-sdk";

export interface Servers {
  /** Alias -> how many tools it gives. */
  up: Record<string, number>;
  /** Alias -> why it could not be reached. */
  down: Record<string, string>;
}

export function serversOf(records: PluginRecord[]): Servers {
  let up: Record<string, number> = {};
  let down: Record<string, string> = {};
  for (const { kind, data } of records) {
    const d = (data ?? {}) as Record<string, unknown>;
    if (kind === "listed" && Array.isArray(d.servers)) {
      up = {};
      for (const s of d.servers as { alias?: unknown; tools?: unknown }[]) {
        if (typeof s?.alias === "string") up[s.alias] = Array.isArray(s.tools) ? s.tools.length : 0;
      }
    } else if (kind === "server_down" && typeof d.alias === "string") {
      down = { ...down, [d.alias]: String(d.reason ?? "") };
    } else if (kind === "server_up" && typeof d.alias === "string") {
      down = Object.fromEntries(Object.entries(down).filter(([a]) => a !== d.alias));
    }
  }
  return { up, down };
}

export function activate(ctx: PluginContext): void {
  const Section = () => {
    const { up, down } = serversOf(useAllRecords(ctx));
    const names = [...new Set([...Object.keys(up), ...Object.keys(down)])].sort();
    return (
      <Panel title="MCP servers">
        {names.length ? (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {names.map((n) => (
              <li key={n}>
                <code>{n}</code>{" "}
                {n in down ? (
                  <span className="notice error" style={{ display: "inline" }}>
                    not reached{down[n] ? `: ${down[n]}` : ""}
                  </span>
                ) : (
                  <span className="muted">
                    {up[n]} tool{up[n] === 1 ? "" : "s"}
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">No MCP server in this session.</div>
        )}
      </Panel>
    );
  };
  ctx.slots.add(SLOTS.settings, { key: "mcp", mount: "keyed" }, Section);
}
