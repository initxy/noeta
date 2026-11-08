import { Panel } from "@initxy/ui";
import { SLOTS, type PluginContext } from "@initxy/web-sdk";

export function activate(ctx: PluginContext): () => void {
  return ctx.slots.add(SLOTS.sidebar, { key: "hello", mount: "keyed" }, () => <Panel title="Hello">Task {ctx.client.taskId}</Panel>);
}
