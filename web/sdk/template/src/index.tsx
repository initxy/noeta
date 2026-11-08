import { Panel } from "@initxy/noeta-ui";
import { SLOTS, type PluginContext } from "@initxy/noeta-web-sdk";

export function activate(ctx: PluginContext): () => void {
  return ctx.slots.add(SLOTS.sidebar, { key: "hello", mount: "keyed" }, () => <Panel title="Hello">Task {ctx.client.taskId}</Panel>);
}
