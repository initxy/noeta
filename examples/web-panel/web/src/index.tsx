// The web-panel plugin's web part: a sidebar panel that asks the plugin's
// backend (`ui_call("workspace")`) what is in the task's directory.
import { Button, Notice, Panel } from "@initxy/noeta-ui";
import { SLOTS, type PluginContext } from "@initxy/noeta-web-sdk";
import { useEffect, useState } from "react";

interface Workspace {
  cwd: string;
  entries: string[];
  count: number;
  error?: string;
}

function WorkspacePanel({ ctx }: { ctx: PluginContext }) {
  const [found, setFound] = useState<Workspace | null>(null);
  const [error, setError] = useState("");
  const look = () =>
    ctx.call<Workspace>("workspace").then(
      (w) => {
        setFound(w);
        setError(w.error ?? "");
      },
      (e: unknown) => setError(String(e instanceof Error ? e.message : e)),
    );
  // Looked at again after each reply: the model may have changed the files.
  useEffect(() => ctx.client.watch((r) => r.event.type === "ModelReplied" && void look()), [ctx]);
  return (
    <Panel title="Workspace">
      <div className="web-panel" data-testid="web-panel">
        {found ? (
          <>
            <div className="muted">{found.cwd}</div>
            <div data-testid="web-panel-count">
              {found.count} entr{found.count === 1 ? "y" : "ies"}
            </div>
            <ul>
              {found.entries.slice(0, 12).map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </>
        ) : (
          <div className="muted">What is in this session's directory?</div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        <Button onClick={() => void look()}>{found ? "Look again" : "Look"}</Button>
        <div className="muted" data-testid="web-panel-host">
          {ctx.desktop ? `in the desktop app (${ctx.desktop.platform})` : "in a browser"}
        </div>
      </div>
    </Panel>
  );
}

export function activate(ctx: PluginContext): () => void {
  const remove = ctx.slots.add(SLOTS.sidebar, { key: "workspace", mount: "keyed" }, () => <WorkspacePanel ctx={ctx} />);
  return remove;
}
