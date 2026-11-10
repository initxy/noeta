import { Fragment, memo, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { shownRecords, type Item, type PluginRecordAt, type TaskState } from "../state/transcript";
import { useApi, useTask, useTaskView, type TaskStore } from "../state/useTask";
import { Markdown } from "@initxy/noeta-ui";
import { recordSlot, Slot, SlotRegistry, SlotsContext } from "@initxy/noeta-web-sdk";
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

function Notice({ item }: { item: Extract<Item, { kind: "notice" }> }) {
  const [open, setOpen] = useState(false);
  if (!item.detail) return <div className={`notice ${item.tone}`}>{item.text}</div>;
  return (
    <div className={`notice ${item.tone}`}>
      {item.text}{" "}
      <button className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide details" : "Details"}
      </button>
      {open && <pre className="notice-detail">{item.detail}</pre>}
    </div>
  );
}

/** One item; drawn again only when the item itself changes (the fold keeps
 * every item it did not touch the same object). */
const Row = memo(function Row({ item }: { item: Item }) {
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
      return <Notice item={item} />;
    case "compaction":
      return <Compaction summary={item.summary} />;
  }
});

function Records({ records }: { records?: PluginRecordAt[] }) {
  return records?.map((r) => (
    <Slot key={r.id} name={recordSlot(r.plugin, r.kind)} props={{ record: { seq: r.seq, plugin: r.plugin, kind: r.kind, data: r.data } }} />
  ));
}

const noSlots = new SlotRegistry(() => false);

/** The plugins' records some contribution shows, by where they go. */
function useShownRecords(records: TaskState["records"]): Map<number, PluginRecordAt[]> {
  const registry = useContext(SlotsContext) ?? noSlots;
  const version = useSyncExternalStore(registry.subscribe, () => registry.slotVersion("record:"));
  return useMemo(
    () => shownRecords({ records }, (slot) => registry.contributions(slot).length > 0),
    [records, registry, version], // version: what the registry holds for records
  );
}

/** Items drawn at first, from the end; the earlier ones follow in batches while the page is idle. */
const FIRST = 40;
const BATCH = 30;

const idle = (run: () => void): (() => void) => {
  if (typeof requestIdleCallback === "function") {
    const id = requestIdleCallback(run, { timeout: 400 });
    return () => cancelIdleCallback(id);
  }
  const timer = setTimeout(run, 30);
  return () => clearTimeout(timer);
};

/** The recorded items: a long conversation draws its last items first and
 * the earlier ones while the page is idle, keeping what is on screen in place. */
const Items = memo(function Items({ items, records: all }: { items: Item[]; records: TaskState["records"] }) {
  const records = useShownRecords(all);
  // The first item drawn: the last FIRST until the idle batches set it.
  const [from, setFrom] = useState(Infinity);
  const start = Math.min(from, Math.max(0, items.length - FIRST));
  const edge = useRef<HTMLDivElement>(null);
  const edgeAt = useRef(start); // the item the edge marker is drawn before
  const held = useRef<number | null>(null); // the marker's place on screen before earlier items came in

  useEffect(() => {
    if (start === 0) return;
    return idle(() => {
      held.current = edge.current?.getBoundingClientRect().top ?? null;
      setFrom(Math.max(0, start - BATCH));
    });
  }, [start]);

  useLayoutEffect(() => {
    // Earlier items came in above: scroll by as much, so what is on screen stays.
    const top = held.current;
    held.current = null;
    const marker = edge.current;
    const scroller = marker?.closest(".scroll");
    if (top === null || !marker || !scroller) return;
    scroller.scrollTop += marker.getBoundingClientRect().top - top;
  }, [start]);

  const markAt = held.current !== null ? edgeAt.current : start;
  edgeAt.current = markAt;
  const rows = [];
  if (start === 0) rows.push(<Records key="r0" records={records.get(0)} />);
  for (let i = start; i < items.length; i++) {
    const item = items[i];
    if (i === markAt) rows.push(<div key="edge" ref={edge} className="window-edge" />);
    rows.push(
      <Fragment key={item.id}>
        <Row item={item} />
        <Records records={records.get(i + 1)} />
      </Fragment>,
    );
  }
  return rows;
});

interface TailState {
  live: TaskState["live"];
  queued: TaskState["queued"];
  busy: boolean;
  waitingOn: string | null;
}

const tailOf = (s: TaskState): TailState => ({ live: s.live, queued: s.queued, busy: s.busy, waitingOn: s.waitingOn });

/** What the model is writing now, the messages waiting their turn, and whether it works. */
function Tail({ live, queued, busy, waitingOn, nested }: TailState & { nested?: boolean }) {
  return (
    <>
      {live?.thinking && !live.text && <Thinking text={live.thinking} live />}
      {live?.text && (
        <div className="msg assistant streaming" data-testid="streaming">
          <Markdown text={live.text} />
        </div>
      )}
      {!nested &&
        queued.map((q) => (
          <div key={q.seq} className="msg user queued">
            <div className="bubble">{q.text}</div>
            <span className="muted">queued</span>
          </div>
        ))}
      {busy && !live?.text && !nested && waitingOn !== "approval" && waitingOn !== "question" && (
        <div className="working" data-testid="working">
          <span className="spinner" /> Working…
        </div>
      )}
    </>
  );
}

function StoreTail({ store, nested }: { store: TaskStore; nested?: boolean }) {
  return <Tail {...useTaskView(store, tailOf)} nested={nested} />;
}

const itemsOf = (s: TaskState) => s.items;
const recordsOf = (s: TaskState) => s.records;

/** The conversation a store follows: each part reads only what it draws, so
 * streamed text re-renders the streaming row alone. */
export function LiveTranscript({ store, nested }: { store: TaskStore; nested?: boolean }) {
  const items = useTaskView(store, itemsOf, Object.is);
  const records = useTaskView(store, recordsOf, Object.is);
  return (
    <div className={nested ? "transcript nested-transcript" : "transcript"} aria-live="polite">
      <Items items={items} records={records} />
      <StoreTail store={store} nested={nested} />
    </div>
  );
}

/** Another task's conversation (a sub-agent's), followed live: plugins get
 * it as `ctx.Transcript`. */
export function TaskTranscript({ taskId }: { taskId: string }) {
  const store = useTask(useApi(), taskId);
  return (
    <div className="nested">
      <LiveTranscript store={store} nested />
    </div>
  );
}

/** The conversation: recorded items, then what the model is writing now. */
export function Transcript({ state, nested }: { state: TaskState; nested?: boolean }) {
  return (
    <div className={nested ? "transcript nested-transcript" : "transcript"} aria-live="polite">
      <Items items={state.items} records={state.records} />
      <Tail {...tailOf(state)} nested={nested} />
    </div>
  );
}
