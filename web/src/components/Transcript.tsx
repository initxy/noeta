import { Fragment, useContext, useMemo, useState, useSyncExternalStore } from "react";
import { shownRecords, type Item, type PluginRecordAt, type TaskState } from "../state/transcript";
import { useApi, useTask } from "../state/useTask";
import { Markdown } from "@noeta-ai/ui";
import { recordSlot, Slot, SlotRegistry, SlotsContext } from "@noeta-ai/web-sdk";
import { ToolCard } from "./ToolCard";

function Thinking({ text, live }: { text: string; live?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="thinking">
      <button className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
        {live ? "Thinking…" : "Thought"} {open ? "▾" : "▸"}
      </button>
      {open && <div className="thinking-text">{text}</div>}
    </div>
  );
}

function Compaction({ summary }: { summary: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="compaction">
      <button className="link" onClick={() => setOpen(!open)}>
        Conversation compacted {open ? "▾" : "▸"}
      </button>
      {open && <Markdown text={summary} />}
    </div>
  );
}

function Row({ item }: { item: Item }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="msg user">
          <div className="bubble">
            {item.text}
            {item.images.map((src, i) => (
              <img key={i} src={src} alt={`attached image ${i + 1}`} className="thumb" />
            ))}
          </div>
        </div>
      );
    case "assistant":
      return (
        <div className="msg assistant">
          <Markdown text={item.text} />
        </div>
      );
    case "thinking":
      return <Thinking text={item.text} />;
    case "tool":
      return <ToolCard t={item} />;
    case "notice":
      return <div className={`notice ${item.tone}`}>{item.text}</div>;
    case "compaction":
      return <Compaction summary={item.summary} />;
  }
}

function Records({ records }: { records?: PluginRecordAt[] }) {
  return records?.map((r) => (
    <Slot key={r.id} name={recordSlot(r.plugin, r.kind)} props={{ record: { seq: r.seq, plugin: r.plugin, kind: r.kind, data: r.data } }} />
  ));
}

const noSlots = new SlotRegistry(() => false);

/** The plugins' records some contribution shows, by where they go. */
function useShownRecords(state: TaskState): Map<number, PluginRecordAt[]> {
  const registry = useContext(SlotsContext) ?? noSlots;
  const version = useSyncExternalStore(registry.subscribe, () => registry.version);
  return useMemo(
    () => shownRecords(state, (slot) => registry.contributions(slot).length > 0),
    [state.records, registry, version], // version: what the registry holds
  );
}

/** Another task's conversation (a sub-agent's), followed live: plugins get
 * it as `ctx.Transcript`. */
export function TaskTranscript({ taskId }: { taskId: string }) {
  const [state] = useTask(useApi(), taskId);
  return (
    <div className="nested">
      <Transcript state={state} nested />
    </div>
  );
}

/** The conversation: recorded items, then what the model is writing now. */
export function Transcript({ state, nested }: { state: TaskState; nested?: boolean }) {
  const { live } = state;
  const records = useShownRecords(state);
  return (
    <div className={nested ? "transcript nested-transcript" : "transcript"} aria-live="polite">
      <Records records={records.get(0)} />
      {state.items.map((item, i) => (
        <Fragment key={item.id}>
          <Row item={item} />
          <Records records={records.get(i + 1)} />
        </Fragment>
      ))}
      {live?.thinking && !live.text && <Thinking text={live.thinking} live />}
      {live?.text && (
        <div className="msg assistant streaming" data-testid="streaming">
          <Markdown text={live.text} />
        </div>
      )}
      {!nested &&
        state.queued.map((q) => (
          <div key={q.seq} className="msg user queued">
            <div className="bubble">{q.text}</div>
            <span className="muted">queued</span>
          </div>
        ))}
      {state.busy && !live?.text && !nested && state.waitingOn !== "approval" && state.waitingOn !== "question" && (
        <div className="working" data-testid="working">
          <span className="spinner" /> Working…
        </div>
      )}
    </div>
  );
}
