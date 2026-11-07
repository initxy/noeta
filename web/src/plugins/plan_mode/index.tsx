// The plan-mode plugin's web part: the plan to review on the question card
// `ExitPlanMode` asks, and the plan on its call's card once answered. (Its
// mode switches are the permissions plugin's records, shown there.)
import { Markdown } from "@noeta-ai/ui";
import { requestSlot, toolSlot, type PluginContext, type RequestSlotProps, type ToolSlotProps, type WrapProps } from "@noeta-ai/web-sdk";

const TOOL = "ExitPlanMode";

const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/** The plan in place of the card's lead (who asks), when there is one. */
function Review({ request, children }: RequestSlotProps & WrapProps) {
  if (!str(request.input.plan)) return <>{children}</>;
  return (
    <>
      <div className="card-title">Ready to code? Here is the plan:</div>
      <div className="plan">
        <Markdown text={str(request.input.plan)} />
      </div>
    </>
  );
}

/** While the call waits, the plan is on the review card. */
function Plan({ call }: ToolSlotProps) {
  return call.status === "done" || call.status === "error" ? <Markdown text={str(call.input.plan)} /> : null;
}

export function activate(ctx: PluginContext): void {
  ctx.slots.add(requestSlot(TOOL), { mount: "single" }, Review);
  ctx.slots.add(toolSlot(TOOL), { mount: "single" }, Plan);
}
