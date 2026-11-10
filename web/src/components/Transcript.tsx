import { Fragment, memo, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { shownRecords, type Item, type PluginRecordAt, type TaskState } from "../state/transcript";
import { useApi, useTask, useTaskView, type TaskStore } from "../state/useTask";
import { agentsSummary, editsSummary, groupItems, groupSummary, Markdown, poppedOut, type Block } from "@initxy/noeta-ui";
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

const sameItems = (a: Item[], b: Item[]) => a.length === b.length && a.every((x, i) => x === b[i]);

/** A run of quiet calls: one line saying what they did, opened on a click;
 * under it the latest text between them, the command running now, and the
 * calls that failed or wait for you. */
const GroupRow = memo(
  function GroupRow({ items }: { items: Item[] }) {
    const [open, setOpen] = useState(false);
    const s = groupSummary(items);
    const popped = items.filter((i): i is Tool => i.kind === "tool" && poppedOut(i));
    return (
      <div className="tool-group" data-status={s.active ? "running" : "done"}>
        <button className="group-head" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className={`dot dot-${s.active ? "running" : "done"}`} aria-hidden />
          <span className="group-line">{s.line}</span>
          {s.failed > 0 && <span className="group-failed">{s.failed} failed</span>}
          <span className="group-caret" aria-hidden>
            {open ? "▾" : "▸"}
          </span>
        </button>
        {open ? (
          <div className="group-body">
            {items.map((item) => (
              <Row key={item.id} item={item} />
            ))}
          </div>
        ) : (
          <>
            {(s.text || (s.active && s.hint)) && (
              <div className="group-under">
                {s.text && <div className="group-text">{s.text}</div>}
                {s.active && s.hint && <div className="group-hint">{s.hint}</div>}
              </div>
            )}
            {popped.length > 0 && (
              <div className="group-body">
                {popped.map((t) => (
                  <ToolCard key={t.id} t={t} />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    );
  },
  (a, b) => sameItems(a.items, b.items),
);

/** Edits of one file in a row: one line, each edit's diff once opened. */
const EditsRow = memo(
  function EditsRow({ path, calls }: { path: string; calls: Tool[] }) {
    const [open, setOpen] = useState(false);
    return (
      <div className="tool-group">
        <button className="group-head" onClick={() => setOpen(!open)} aria-expanded={open} title={path}>
          <span className={`dot dot-${calls.some((t) => t.status === "running" || t.status === "pending") ? "running" : "done"}`} aria-hidden />
          <span className="group-line">{editsSummary(path, calls)}</span>
          <span className="group-caret" aria-hidden>
            {open ? "▾" : "▸"}
          </span>
        </button>
        {open && (
          <div className="group-body">
            {calls.map((t) => (
              <ToolCard key={t.id} t={t} />
            ))}
          </div>
        )}
      </div>
    );
  },
  (a, b) => a.path === b.path && sameItems(a.calls, b.calls),
);

/** Agents started together: one line, then a line per agent. */
const AgentsRow = memo(
  function AgentsRow({ calls }: { calls: Tool[] }) {
    const summary = agentsSummary(calls);
    return (
      <div className="tool-group">
        <div className="group-head">
          <span className={`dot dot-${summary.startsWith("Running") ? "running" : "done"}`} aria-hidden />
          <span className="group-line">{summary}</span>
        </div>
        <div className="group-body">
          {calls.map((t) => (
            <ToolCard key={t.id} t={t} />
          ))}
        </div>
      </div>
    );
  },
  (a, b) => sameItems(a.calls, b.calls),
);

type Tool = Extract<Item, { kind: "tool" }>;

function BlockRow({ block }: { block: Block<Item> }) {
  switch (block.kind) {
    case "item":
      return <Row item={block.item} />;
    case "group":
      return <GroupRow items={block.items} />;
    case "edits":
      return <EditsRow path={block.path} calls={block.calls} />;
    case "agents":
      return <AgentsRow calls={block.calls} />;
    case "skip":
      return null;
  }
}

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

/** Blocks drawn at first, from the end; the earlier ones follow in batches while the page is idle. */
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

/** The recorded items, runs of calls folded (groupItems): a long
 * conversation draws its last blocks first and the earlier ones while the
 * page is idle, keeping what is on screen in place. */
const Items = memo(function Items({ items: all, records: kept }: { items: Item[]; records: TaskState["records"] }) {
  const records = useShownRecords(kept);
  const items = useMemo(() => groupItems(all), [all]);
  // The first item drawn: the last FIRST until the idle batches set it.
  const [from, setFrom] = useState(Infinity);
  const start = Math.min(from, Math.max(0, items.length - FIRST));
  const edge = useRef<HTMLDivElement>(null);
  const edgeAt = useRef(start); // the block the edge marker is drawn before
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
    const block = items[i];
    if (i === markAt) rows.push(<div key="edge" ref={edge} className="window-edge" />);
    const after: PluginRecordAt[] = [];
    for (let at = block.from + 1; at <= block.to; at++) after.push(...(records.get(at) ?? []));
    rows.push(
      <Fragment key={block.id}>
        <BlockRow block={block} />
        {after.length > 0 && <Records records={after} />}
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
          <Markdown text={live.text} streaming />
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
