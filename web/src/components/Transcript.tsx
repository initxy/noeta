import { useState } from "react";
import type { Item, TaskState } from "../state/transcript";
import { Markdown } from "@noeta-ai/ui";
import { recordSlot, Slot } from "@noeta-ai/web-sdk";
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
    case "record":
      return <Slot name={recordSlot(item.plugin, item.recordKind)} props={{ record: { seq: item.seq, plugin: item.plugin, kind: item.recordKind, data: item.data } }} />;
  }
}

/** The conversation: recorded items, then what the model is writing now. */
export function Transcript({ state, nested }: { state: TaskState; nested?: boolean }) {
  const { live } = state;
  return (
    <div className={nested ? "transcript nested-transcript" : "transcript"} aria-live="polite">
      {state.items.map((item) => (
        <Row key={item.id} item={item} />
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
