import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Layers,
  ListChecks,
  Terminal,
  User,
  Workflow,
  X,
} from "lucide-react";
import { Fragment, memo, useEffect, useMemo, useState } from "react";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "../components/ai-elements/message.jsx";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "../components/ai-elements/reasoning.jsx";
import {
  Tool,
  ToolContent,
  ToolHeader,
  ToolInput,
  ToolOutput,
} from "../components/ai-elements/tool.jsx";
import { Markdown } from "../components/markdown.jsx";
import { formatClock, safeJson } from "../lib/format.js";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { imageSrcFor, userMessageImages } from "./chat-images.js";
import { answersComplete, applyAnswerPatch } from "./question-answers.js";

function Transcript({
  activeTaskId,
  events,
  ensureMessageBodiesBatch,
  ensureThinkingBatch,
  localImageCache,
  messageFullCache,
  messageTextCache,
  onOpenImage,
  onOpenSubtask,
  pendingGoalText,
  responseThinkingCache,
  streamingTurn,
  vm,
}) {
  // Self-manage lazy body/thinking derefs scoped to THIS transcript's task, so
  // the same component serves both the main conversation and the subtask popup
  // (each fetches content with its own task_id).
  //
  // Perf fix #1 — collect ALL message/response hashes the transcript needs in
  // one pass, then hand the whole list to the BATCH derefs. Each batch fetches
  // every still-missing body in parallel and folds them into its cache with a
  // SINGLE setState, so backfilling an N-message history costs O(1) re-renders
  // instead of ~2N (the "bubbling up one row at a time" cause). The live SSE path appends tokens
  // to events directly and never routes a single new message through here.
  // Precompute the LLMResponseRecorded (seq, hash) pairs ONCE per events change,
  // sorted by seq, so the per-turn "latest response before this turn" lookup is a
  // binary search instead of a full O(events) scan — the backfill effect below
  // then runs in O(turns·log events) instead of O(turns·events).
  const responseRefSeqIndex = useMemo(() => buildResponseRefSeqIndex(events), [events]);
  useEffect(() => {
    if (!ensureMessageBodiesBatch) return;
    const bodyHashes = [];
    const thinkingHashes = [];
    for (const turn of vm.turns) {
      if (turn.kind === "message" && turn.messagesRef) {
        bodyHashes.push(turn.messagesRef);
        const responseHash = responseRefHashForSeqIndex(responseRefSeqIndex, turn.seq);
        if (responseHash) thinkingHashes.push(responseHash);
      }
    }
    if (bodyHashes.length) ensureMessageBodiesBatch(bodyHashes, activeTaskId);
    if (thinkingHashes.length && ensureThinkingBatch)
      ensureThinkingBatch(thinkingHashes, activeTaskId);
  }, [vm.turns, responseRefSeqIndex, ensureMessageBodiesBatch, ensureThinkingBatch, activeTaskId]);

  // Perf fix #2 — buildGroups walks every turn AND derefs the body/text caches
  // per turn; memoize it so it only re-runs when the turns or those caches
  // actually change, not on every unrelated re-render.
  const groups = useMemo(
    () => buildGroups(vm.turns, messageFullCache, messageTextCache),
    [vm.turns, messageFullCache, messageTextCache],
  );
  const hasResolvedUser = groups.some((group) => group.kind === "user");
  const timeBySeq = useMemo(() => buildTimeBySeq(events), [events]);
  // Perf fix #2 — ctx is the shared bag every assistant bubble reads. It was a
  // fresh object literal on every render, which defeated AssistantTurn's
  // React.memo (a new ctx === new prop). Memoize it over its stable members so
  // assistant bubbles only re-render when something they actually depend on
  // changes.
  //
  // WS-A / P0-2 — carry the memoized binary-search `responseRefSeqIndex` instead
  // of the raw `events` array. `events` got a fresh identity on every envelope
  // (so ctx churned every stream tick); the index only changes when an
  // LLMResponseRecorded actually arrives, and it turns the per-message lookup in
  // renderTurnItem from an O(events) scan into O(log n).
  const ctx = useMemo(
    () => ({
      activeTaskId,
      messageFullCache,
      messageTextCache,
      onOpenImage,
      onOpenSubtask,
      responseRefIndex: responseRefSeqIndex,
      responseThinkingCache,
      vm,
    }),
    [
      activeTaskId,
      messageFullCache,
      messageTextCache,
      onOpenImage,
      onOpenSubtask,
      responseRefSeqIndex,
      responseThinkingCache,
      vm,
    ],
  );

  return (
    <>
      {pendingGoalText && !hasResolvedUser ? <UserMessage text={pendingGoalText} /> : null}
      {groups.map((group, index) =>
        group.kind === "user" ? (
          // Render the bubble if either text or images is non-empty (image-only
          // user messages still get a bubble).
          group.text || (group.images && group.images.length) ? (
            <UserMessage
              key={`u-${group.turn.seq ?? index}`}
              text={group.text}
              images={group.images}
              taskId={activeTaskId}
              localImageCache={localImageCache}
              onOpenImage={onOpenImage}
              ts={timeBySeq.get(group.turn.seq)}
            />
          ) : null
        ) : (
          <AssistantTurn
            ctx={ctx}
            items={group.items}
            key={`a-${group.items[0]?.seq ?? index}`}
            ts={timeBySeq.get(group.items[0]?.seq)}
          />
        ),
      )}
      {/* Token streaming: the ephemeral preview of the assistant turn in
          flight, painted AFTER the last durable group. streamingTurn is null
          until the delta buffer has visible content, and clears in the same
          update as the MessagesAppended that supersedes it — the seq-keyed
          final bubble takes over in one repaint. Only the ROOT task's turn is
          passed in (subtask previews are a v1 non-goal). */}
      {streamingTurn ? (
        <StreamingAssistantTurn
          blocks={streamingTurn.blocks}
          key={`stream-${streamingTurn.callId}`}
        />
      ) : null}
    </>
  );
}

// seq → occurred_at (Unix seconds). Every envelope carries `occurred_at`; the
// renderer keys a bubble's timestamp off the originating turn's seq.
function buildTimeBySeq(events) {
  const map = new Map();
  for (const env of events) {
    if (env && typeof env.seq === "number") map.set(env.seq, env.occurred_at);
  }
  return map;
}

// Shared by both bubbles: a small muted wall-clock stamp, hidden when the time
// is unknown (e.g. an optimistic pending bubble with no event yet).
function BubbleTime({ ts }) {
  const label = formatClock(ts);
  return label ? <time className="bubble-time">{label}</time> : null;
}

// The role label on the bubble (replacing the old avatar beside it). Sits on a
// top row inside the bubble: icon + short label. It no longer takes a column of
// width outside the bubble, so the bubble aligns to the same column as the input
// box below.
function BubbleRole({ role }) {
  return (
    <span className={`bubble-role bubble-role--${role}`}>
      {role === "user" ? <User size={ICON_SM} /> : <Bot size={ICON_SM} />}
      {role === "user" ? "You" : "Noeta"}
    </span>
  );
}

// Fold the flat, ordered turn timeline into render groups: one bubble per
// conversational round. A user message closes the current assistant round and
// opens its own bubble; every other renderable turn (assistant prose, tool
// calls, thinking, lifecycle markers) accretes onto the in-flight assistant
// round. This is the "renderer groups consecutive turns into bubbles" contract
// the reducer documents but deliberately leaves to the view layer.
function buildGroups(turns, fullCache, textCache) {
  const groups = [];
  let current = null;
  for (const turn of turns) {
    const isUser =
      turn.kind === "message" && messageRole(turn, fullCache, textCache) === "user";
    if (isUser) {
      if (current) {
        groups.push(current);
        current = null;
      }
      groups.push({
        kind: "user",
        turn,
        text: userMessageText(turn, fullCache, textCache),
        // images this user message carries ([{hash, mediaType}], in order of appearance).
        images: userMessageImageList(turn, fullCache, textCache),
      });
      continue;
    }
    if (turn.kind === "model") continue;
    if (turn.kind === "lifecycle" && turn.label === "suspended") continue;
    if (!current) current = { kind: "assistant", items: [] };
    current.items.push(turn);
  }
  if (current) groups.push(current);
  return groups;
}

// Perf fix #2 — React.memo on the user bubble. Effective now that every prop is
// a stable reference: text/images ride the memoized buildGroups output,
// onOpenImage is a stable callback, localImageCache is state. So a user bubble
// whose inputs are unchanged skips re-render during a history backfill.
const UserMessage = memo(function UserMessage({
  text,
  images,
  taskId,
  localImageCache,
  onOpenImage,
  ts,
}) {
  // Resolve this message's image blocks into a
  // renderable src: on a local-cache hit (the moment it was just sent) use a data
  // URL, zero requests; otherwise build the backend fetch-image route (history
  // reload). Both paths converge on the same hash key here — see
  // chat-images.imageSrcFor.
  const thumbs = (Array.isArray(images) ? images : [])
    .map((img) => ({
      hash: img.hash,
      mediaType: img.mediaType,
      src: imageSrcFor(img.hash, taskId, localImageCache),
    }))
    .filter((t) => t.src);
  return (
    <Message from="user">
      <MessageContent>
        <BubbleRole role="user" />
        {text ? <MessageResponse>{text}</MessageResponse> : null}
        {thumbs.length ? (
          <div className="bubble-images" aria-label="Attached images">
            {thumbs.map((thumb) => (
              <button
                type="button"
                className="bubble-image-thumb"
                key={thumb.hash}
                title="Click to zoom"
                aria-label="Zoom in on image"
                onClick={() => onOpenImage?.(thumb.src)}
              >
                <img src={thumb.src} alt="User-uploaded image" loading="lazy" />
              </button>
            ))}
          </div>
        ) : null}
        <div className="user-bubble-foot">
          <BubbleTime ts={ts} />
        </div>
      </MessageContent>
    </Message>
  );
});

// One assistant round rendered as a single bubble: a lone role chip, then the
// round's items (thinking, prose, tool cards, markers) in timeline order. If
// every item resolves to nothing (e.g. a message whose body has not loaded
// yet), the whole bubble is suppressed so no empty "Assistant" chip flashes.
// Perf fix #2 — React.memo on the assistant bubble. Effective now that ``ctx``
// is memoized in Transcript and ``items`` rides the memoized ``buildGroups``
// output: during a history backfill, an assistant bubble whose inputs are
// unchanged skips re-render entirely (and with it the markdown re-parse).
const AssistantTurn = memo(function AssistantTurn({ ctx, items, ts }) {
  // Coalesce runs of back-to-back tool calls into a single collapsed group so a
  // burst of tools reads as one "ran N tools" line; any non-tool item (prose,
  // thinking, a lifecycle marker) closes the current run. A lone tool stays a
  // bare card — grouping a single call would only add chrome.
  const children = [];
  let toolRun = [];
  const flushTools = () => {
    if (!toolRun.length) return;
    const calls = toolRun.map((turn) => ctx.vm.toolCalls[turn.callId]).filter(Boolean);
    if (calls.length) {
      children.push(
        <ToolGroup
          activeTaskId={ctx.activeTaskId}
          calls={calls}
          diffs={ctx.vm.diffs}
          images={ctx.vm.images}
          onOpenImage={ctx.onOpenImage}
          key={`tg-${toolRun[0].callId}`}
        />,
      );
    }
    toolRun = [];
  };
  items.forEach((turn, index) => {
    if (turn.kind === "tool") {
      toolRun.push(turn);
      return;
    }
    flushTools();
    const node = renderTurnItem(turn, ctx, index);
    if (node) children.push(node);
  });
  flushTools();

  if (!children.length) return null;
  return (
    <Message from="assistant">
      <MessageContent>
        <BubbleRole role="assistant" />
        {children}
        <BubbleTime ts={ts} />
      </MessageContent>
    </Message>
  );
});

// The live assistant bubble for an in-flight LLM call, painted from the delta
// buffer (ADR token-streaming-projection.md). Blocks arrive ordered by
// content-block index: text through the same Markdown renderer as final prose,
// thinking through the same Reasoning disclosure held open while tokens flow.
// The caller keys it `stream-${callId}`, so the next call in a tool loop (or a
// retried call) remounts a fresh bubble. No BubbleTime: a preview has no
// durable envelope to take a timestamp from. React.memo holds across
// re-renders that are unrelated to the stream because chat-data memoizes the
// streaming turn (blocks identity changes only when a delta actually lands).
const StreamingAssistantTurn = memo(function StreamingAssistantTurn({ blocks }) {
  return (
    <Message from="assistant">
      <MessageContent>
        <BubbleRole role="assistant" />
        {blocks.map((block) =>
          block.kind === "thinking" ? (
            <ThinkingDisclosure key={`sb-${block.index}`} streaming text={block.text} />
          ) : (
            <Markdown key={`sb-${block.index}`} text={block.text} />
          ),
        )}
      </MessageContent>
    </Message>
  );
});

function renderTurnItem(turn, ctx, key) {
  const { messageFullCache, messageTextCache, responseRefIndex, responseThinkingCache } = ctx;

  if (turn.kind === "message") {
    const responseHash = responseRefHashForSeqIndex(responseRefIndex, turn.seq);
    const thinking = responseHash ? responseThinkingCache.get(responseHash) || [] : [];
    const parts = assistantMessageParts(turn, messageFullCache, messageTextCache);
    if (!thinking.length && !parts.length) return null;
    return (
      <Fragment key={`m-${turn.seq ?? key}`}>
        {thinking.map((text, index) => (
          <ThinkingDisclosure key={`t-${index}`} text={text} />
        ))}
        {parts.map((part, index) =>
          part.type === "thinking" ? (
            <ThinkingDisclosure key={`p-${index}`} text={part.text} />
          ) : (
            <Markdown key={`p-${index}`} text={part.text} />
          ),
        )}
      </Fragment>
    );
  }

  if (turn.kind === "assistant_text") {
    return turn.text ? <Markdown key={`at-${turn.seq ?? key}`} text={turn.text} /> : null;
  }

  // turn.kind === "tool" is handled upstream in AssistantTurn (consecutive
  // tools are coalesced into a ToolGroup), so it never reaches here.

  return <EventMarker key={`e-${turn.seq ?? key}`} ctx={ctx} turn={turn} />;
}

function ThinkingDisclosure({ text, streaming = false }) {
  // U11 — a durable thinking block is already complete when it renders, so it
  // keeps the collapsed default; the char-count hint + the quieter reasoning
  // styling (rail / wash / fade-in) live in CSS. A STREAMING block (the live
  // preview bubble) is still receiving tokens: hold the disclosure open so the
  // reasoning is visible as it flows — the `open` prop rides Reasoning's prop
  // spread onto the underlying <details>, overriding its internal state, and
  // the whole preview bubble unmounts on handover to the final content.
  const chars = (text || "").length;
  const streamingProps = streaming ? { isStreaming: true, open: true } : {};
  return (
    <Reasoning {...streamingProps}>
      <ReasoningTrigger meta={`${chars.toLocaleString()} chars`} />
      <ReasoningContent>{text}</ReasoningContent>
    </Reasoning>
  );
}

function EventMarker({ ctx, turn }) {
  if (turn.kind === "subtask") {
    return <SubtaskChip ctx={ctx} turn={turn} />;
  }
  if (turn.kind === "warning" && turn.label === "llm-retry") {
    // One marker per retried LLM call, updated in place by the reducer as
    // attempts accumulate — reads as the episode's summary once it's over.
    const detail = turn.error ? ` — ${clip(turn.error, 80)}` : "";
    return (
      <div className="turn-marker">
        {`Provider error, retried ${turn.attempt}/${turn.maxRetries}${detail}`}
      </div>
    );
  }
  if (turn.kind === "skill_loaded") {
    const skills = Array.isArray(turn.skills) ? turn.skills.filter(Boolean) : [];
    const label = skills.length === 1
      ? `Loaded skill: ${skills[0]}`
      : `Loaded skills: ${skills.join(", ")}`;
    return <div className="turn-marker">{label}</div>;
  }
  const label =
    turn.kind === "approval"
      ? `Awaiting approval to run ${turn.toolName}`
      : turn.kind === "approval_resolved"
        ? `${turn.approved ? "Approved" : "Denied"} ${turn.toolName || ""}`
        : turn.kind === "denied"
          ? `Blocked by guard: ${turn.toolName || "tool"}`
          : turn.detail
            ? `${turn.label || turn.kind}: ${turn.detail}`
            : turn.label || turn.kind;
  return <div className="turn-marker">{label}</div>;
}

// Map the reserved orchestration agent name to a friendly label; everything
// else is an ordinary roster sub-agent shown by its own name.
function friendlyAgentName(name) {
  if (name === "__workflow__") return "Workflow";
  return name || "agent";
}

function clip(text, max) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

// A clickable delegation chip in the transcript: friendly name + a one-line
// task-prompt preview + a live status dot (running → done). Click opens the
// subtask drill-in popup. A "__workflow__" node gets the Workflow icon.
function SubtaskChip({ ctx, turn }) {
  const sub = ctx.vm.subtasks?.[turn.subtaskId];
  const status = sub?.status || "running";
  const isWorkflow = turn.agentName === "__workflow__";
  const open = ctx.onOpenSubtask;
  const title =
    status === "failed"
      ? `Failed: ${sub?.error || "sub-agent failed"}`
      : turn.goal || "";
  return (
    <button
      className={`subtask-chip status-${status}`}
      type="button"
      disabled={!open}
      title={title}
      onClick={() =>
        open?.({
          taskId: turn.subtaskId,
          agentName: turn.agentName,
          goal: turn.goal,
        })
      }
    >
      <span className="subtask-chip__icon">
        {isWorkflow ? <Workflow size={ICON_SM} /> : <Bot size={ICON_SM} />}
      </span>
      <span className="subtask-chip__name">{friendlyAgentName(turn.agentName)}</span>
      {turn.goal ? (
        <span className="subtask-chip__goal">{clip(turn.goal, 64)}</span>
      ) : null}
      <span className={`subtask-chip__dot ${status}`} aria-hidden="true" />
    </button>
  );
}

// A strip above the composer aggregating this conversation's still-running
// delegations, so an in-flight workflow/sub-agent is always visible (and
// clickable into its popup) even when the spawn chip scrolled out of view.
function RunningStrip({ vm, onOpen }) {
  const running = Object.entries(vm.subtasks || {})
    .filter(([, sub]) => sub.status === "running")
    .map(([taskId, sub]) => ({ taskId, ...sub }));
  if (!running.length) return null;
  return (
    <div className="running-strip">
      {running.map((sub) => (
        <button
          className="running-chip"
          key={sub.taskId}
          type="button"
          onClick={() =>
            onOpen({ taskId: sub.taskId, agentName: sub.agentName, goal: sub.goal })
          }
        >
          <span className="running-chip__spin" aria-hidden="true" />
          {sub.agentName === "__workflow__" ? (
            <Workflow size={ICON_SM} />
          ) : (
            <Bot size={ICON_SM} />
          )}
          <span>{friendlyAgentName(sub.agentName)} running</span>
        </button>
      ))}
    </div>
  );
}

// A session-level strip (sibling of RunningStrip) showing the model's current
// todo_write checklist (folded vm.todos: CW18b replace-all {id, content,
// status}). Collapsed by default to a one-line summary; click the header to
// expand the full list. Hidden when there are no todos. Pure read off vm — no
// backend round-trip; it updates live as TaskStatePatched(set_todos) arrives.
function TodoStrip({ vm }) {
  const [open, setOpen] = useState(false);
  const todos = Array.isArray(vm.todos) ? vm.todos : [];
  if (!todos.length) return null;
  const done = todos.filter((todo) => todo.status === "completed").length;
  const active = todos.filter((todo) => todo.status === "in_progress").length;
  return (
    <div className={`todo-strip${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="todo-strip__header"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? <ChevronDown size={ICON_SM} /> : <ChevronRight size={ICON_SM} />}
        <ListChecks size={ICON_SM} />
        <span className="todo-strip__label">Todos</span>
        <span className="todo-strip__counts">
          {done}/{todos.length} done{active ? ` · ${active} in progress` : ""}
        </span>
      </button>
      {open ? (
        <ul className="todo-strip__list">
          {todos.map((todo, index) => (
            <li
              className={`todo-item status-${todo.status || "pending"}`}
              key={todo.id || index}
            >
              <span className="todo-item__mark" aria-hidden="true">
                {todo.status === "completed" ? (
                  <Check size={ICON_SM} />
                ) : todo.status === "in_progress" ? (
                  <span className="todo-item__spin" />
                ) : (
                  <Circle size={ICON_SM} />
                )}
              </span>
              <span className="todo-item__text">{todo.content || ""}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// A short, single-line preview of a background command for a chip label.
function commandPreview(command) {
  return clip(command || "shell", 48);
}

// One chip per background shell job: a terminal glyph, the command preview, and
// a status dot — a spinner while running, a solid done/killed dot once terminal
// (mirrors SubtaskChip's status dot). Click → drill into the job's output.
function BackgroundJobChip({ job, onOpen }) {
  const status = job.status || "running";
  const title =
    status === "killed"
      ? `killed${job.signal ? ` (signal ${job.signal})` : ""}: ${job.command}`
      : status === "exited"
        ? `exited${typeof job.exitCode === "number" ? ` (${job.exitCode})` : ""}: ${job.command}`
        : status === "lost"
          ? `lost: ${job.command}`
          : `running: ${job.command}`;
  return (
    <button
      className={`background-job-chip status-${status}`}
      type="button"
      title={title}
      onClick={() => onOpen(job)}
    >
      {status === "running" ? (
        <span className="background-job-chip__spin" aria-hidden="true" />
      ) : (
        <span className={`background-job-chip__dot ${status}`} aria-hidden="true" />
      )}
      <Terminal size={ICON_SM} />
      <span className="background-job-chip__cmd">{commandPreview(job.command)}</span>
    </button>
  );
}

function isRunningBackgroundJob(job) {
  return (job?.status || "running") === "running";
}

// A session-level strip (sibling of RunningStrip) listing this conversation's
// live background shell jobs. Terminal jobs remain in the folded
// event model for audit / an already-open output modal, but the strip hides
// them once they exit, are killed, or are marked lost.
function BackgroundJobsStrip({ jobs, onOpen }) {
  const runningJobs = jobs.filter(isRunningBackgroundJob);
  if (!runningJobs.length) return null;
  return (
    <div className="background-jobs-strip">
      <span className="background-jobs-strip__label">Background processes</span>
      {runningJobs.map((job) => (
        <BackgroundJobChip job={job} key={job.jobId} onOpen={onOpen} />
      ))}
    </div>
  );
}
// Map a tool call's lifecycle to the disclosure dot state shared by the header
// and the group summary (running / done / errored).
function toolState(call) {
  if (call.success === false) return "output-error";
  if (call.status === "recorded" || call.status === "finished") return "output-available";
  return "input-available";
}

function toolIsRunning(call) {
  return toolState(call) === "input-available";
}

// One dot for the whole run: error wins, then still-running, else done.
function groupState(calls) {
  if (calls.some((call) => call.success === false)) return "output-error";
  if (calls.some(toolIsRunning)) return "input-available";
  return "output-available";
}

// A run of consecutive tool calls, folded into one disclosure: collapsed it
// reads "N tools · names" with a status dot per call; expanded it stacks the
// individual cards, each still independently openable. The group
// starts open while any call is still running so live work stays visible, and
// settles closed once everything has resolved. A single call skips the wrapper.
function ToolGroup({ activeTaskId, calls, diffs, images, onOpenImage }) {
  const anyRunning = calls.some(toolIsRunning);
  const [open, setOpen] = useState(anyRunning);

  useEffect(() => {
    setOpen(anyRunning);
  }, [anyRunning]);

  if (calls.length <= 1) {
    const call = calls[0];
    return call ? (
      <ToolCallCard
        activeTaskId={activeTaskId}
        call={call}
        diffs={diffs}
        images={images}
        onOpenImage={onOpenImage}
      />
    ) : null;
  }

  const names = dedupeNames(calls.map((call) => call.toolName || "tool"));
  const errored = calls.filter((call) => call.success === false).length;
  return (
    <details
      className="ai-tool-group"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="ai-tool-group-header" title={`${calls.length} tool calls`}>
        <Layers size={ICON_SM} />
        <span className="ai-tool-group-title">{calls.length} tools</span>
        <span className="ai-tool-group-names">{names.join(" · ")}</span>
        {errored ? <span className="ai-tool-group-error">{errored} failed</span> : null}
        <span className={`ai-tool-dot state-${groupState(calls)}`} aria-hidden="true" />
        <ChevronDown className="ai-disclosure-icon" size={ICON_SM} />
      </summary>
      <div className="ai-tool-group-body">
        {calls.map((call, index) => (
          <ToolCallCard
            activeTaskId={activeTaskId}
            call={call}
            diffs={diffs}
            images={images}
            onOpenImage={onOpenImage}
            key={call.callId || `tool-${index}`}
          />
        ))}
      </div>
    </details>
  );
}

// Collapse a name list to "Bash ×2 · Read · Edit" so the summary stays compact
// when the same tool is called several times in a row.
function dedupeNames(names) {
  const out = [];
  for (const name of names) {
    const last = out[out.length - 1];
    if (last && last.name === name) last.count += 1;
    else out.push({ name, count: 1 });
  }
  return out.map((entry) => (entry.count > 1 ? `${entry.name} ×${entry.count}` : entry.name));
}

function ToolCallCard({ activeTaskId, call, diffs, images, onOpenImage }) {
  const failed = call.success === false;
  const state = toolState(call);
  return (
    <Tool className={failed ? "is-error" : ""}>
      <ToolHeader state={state} title={call.toolName || "tool"}>
        <span className="tool-hint">{toolArgHint(call.arguments)}</span>
      </ToolHeader>
      <ToolContent>
        <ToolInput input={call.arguments} />
        <ToolOutput output={call.summary} errorText={failed ? call.summary : null} />
        {diffs
          .filter((diff) => diff.callId === call.callId)
          .map((diff) => (
            <DiffDisclosure activeTaskId={activeTaskId} diff={diff} key={diff.hash} />
          ))}
        {images
          .filter((img) => img.callId === call.callId)
          .map((img) => (
            <ImageDisclosure image={img} onOpenImage={onOpenImage} key={img.hash} />
          ))}
      </ToolContent>
    </Tool>
  );
}

// P4 — artifacts are content-addressed by hash, so the same diff opened from
// several tool cards (or reopened) is identical bytes. Cache the in-flight promise
// per (task, hash) module-wide: concurrent opens of one hash share a single fetch,
// reopens reuse the resolved text, and only failures evict (so a retry can refetch).
const artifactCache = new Map();
function fetchArtifact(activeTaskId, hash) {
  const key = `${activeTaskId}:${hash}`;
  let promise = artifactCache.get(key);
  if (!promise) {
    promise = fetch(
      `/tasks/${encodeURIComponent(activeTaskId)}/artifacts/${encodeURIComponent(hash)}`,
    )
      .then((resp) => {
        if (!resp.ok) {
          artifactCache.delete(key);
          return `error: HTTP ${resp.status}`;
        }
        return resp.text();
      })
      .catch((error) => {
        artifactCache.delete(key);
        return `error: ${error.message || "fetch failed"}`;
      });
    artifactCache.set(key, promise);
  }
  return promise;
}

function DiffDisclosure({ activeTaskId, diff }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState(null);
  useEffect(() => {
    if (!open || body != null) return;
    let cancelled = false;
    if (!activeTaskId) return undefined;
    fetchArtifact(activeTaskId, diff.hash).then((text) => {
      if (!cancelled) setBody(text);
    });
    return () => {
      cancelled = true;
    };
  }, [activeTaskId, body, diff.hash, open]);
  return (
    <details className="diff-inline" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>proposed diff ({diff.toolName || "edit"})</summary>
      <pre className="diff-body">
        {renderDiffText(body == null ? "loading..." : body)}
      </pre>
    </details>
  );
}

function renderDiffText(body) {
  return String(body)
    .split("\n")
    .slice(0, 2000)
    .map((line, index) => {
      const cls = line.startsWith("+++")
        ? "diff-file"
        : line.startsWith("---")
          ? "diff-file"
          : line.startsWith("@@")
            ? "diff-hunk"
            : line.startsWith("+")
              ? "diff-add"
              : line.startsWith("-")
                ? "diff-del"
                : "diff-ctx";
      return (
        <span className={cls} key={index}>
          {line}
          {"\n"}
        </span>
      );
    });
}

// An image/* artifact (browser screenshot and the like) shown inline under the
// tool call that produced it — the glanceable counterpart to DiffDisclosure. A
// screenshot's value is in *seeing* it, so unlike a diff it renders openly (no
// <details>); the same global content-addressed route the user-image bubbles
// use (/content/{hash}) serves the bytes, and a click opens the shared Lightbox
// for a full-size view.
function ImageDisclosure({ image, onOpenImage }) {
  const src = imageSrcFor(image.hash);
  if (!src) return null;
  const alt = `${image.toolName || "tool"} image`;
  return (
    <div className="tool-image">
      <button
        type="button"
        className="tool-image-thumb"
        title="Click to zoom"
        aria-label="Zoom in on image"
        onClick={() => onOpenImage?.(src)}
      >
        <img src={src} alt={alt} loading="lazy" />
      </button>
    </div>
  );
}

// U5 ① — a human-readable one-line summary of what a gated call will do, so the
// user doesn't have to read raw JSON to decide. Falls back to "tool: key=value".
function approvalSummary(call) {
  const a = (call && call.arguments) || {};
  const name = (call && call.toolName) || "tool";
  switch (name) {
    case "edit":
    case "write": {
      const lines =
        typeof a.content === "string" ? ` · ${a.content.split("\n").length} lines` : "";
      return `Edit ${a.path || ""}${lines}`.trim();
    }
    case "read":
      return `Read ${a.path || ""}`.trim();
    case "shell_run":
    case "bash":
      return `Run ${clip(a.command || "command", 60)}`;
    case "spawn_subagent":
    case "run_workflow":
      return `Spawn subtask ${clip(a.goal || "", 60)}`.trim();
    default: {
      const keys = Object.keys(a);
      return keys.length
        ? `${name}: ${keys[0]}=${clip(String(a[keys[0]]), 40)}`
        : name;
    }
  }
}

// U5 — one gated call. Human summary on top; the raw args collapse into a
// closed <details> so a big payload never pushes the buttons off-screen.
function ApprovalPrompt({ approval, onApprove, onDeny }) {
  const hasArgs = approval.arguments && Object.keys(approval.arguments).length;
  return (
    <section className="approval-prompt">
      <div className="approval-head">
        <AlertTriangle size={ICON_LG} />
        Approval needed
      </div>
      <p className="approval-summary">{approvalSummary(approval)}</p>
      {hasArgs ? (
        <details className="approval-args-details">
          <summary>Argument details</summary>
          <pre className="approval-args">{safeJson(approval.arguments)}</pre>
        </details>
      ) : null}
      <div className="approval-actions">
        <button className="approve-btn" type="button" onClick={onApprove}>
          <Check size={ICON_LG} />
          Approve
        </button>
        <button className="deny-btn" type="button" onClick={onDeny}>
          <X size={ICON_LG} />
          Deny
        </button>
      </div>
    </section>
  );
}

// U5 ③④ — renders all pending approvals + a batch bar when there is more than
// one. Presentational only: the optimistic-hide set, the undo-window timers and
// the deferred commit ALL live in useChatData (task-scoped), so switching /
// closing a session flushes them to the original task instead of dropping
// (blocker 1), and a failed commit restores the card (blocker 2). Single approve
// commits immediately (onResolve); deny + batch go through the undo window (onDefer).
function ApprovalGroup({ approvals, hidden, onResolve, onDefer }) {
  const visible = approvals.filter((a) => !hidden.has(a.callId));
  if (!visible.length) return null;
  const allIds = visible.map((a) => a.callId);
  return (
    <div className="approval-group">
      {visible.map((approval) => (
        <ApprovalPrompt
          approval={approval}
          key={approval.callId}
          onApprove={() => onResolve(approval.callId, true)}
          onDeny={() => onDefer([approval.callId], false)}
        />
      ))}
      {visible.length > 1 ? (
        <div className="approval-batch">
          <span className="approval-batch__label">{visible.length} pending approvals</span>
          <button
            className="approve-btn"
            type="button"
            onClick={() => onDefer(allIds, true)}
          >
            <Check size={ICON_LG} />
            Approve all ({visible.length})
          </button>
          <button
            className="deny-btn"
            type="button"
            onClick={() => onDefer(allIds, false)}
          >
            <X size={ICON_LG} />
            Deny all ({visible.length})
          </button>
        </div>
      ) : null}
    </div>
  );
}

function QuestionPrompt({ pending, onSubmit }) {
  const questions = Array.isArray(pending.questions)
    ? pending.questions.filter((question) => question && question.id)
    : [];
  const [answers, setAnswers] = useState({});
  // B17 / U6 — choice and freeform coexist (merge per-field, never replace).
  const complete = answersComplete(questions, answers);
  return (
    <section className="question-prompt">
      <div className="question-title">{pending.reason || "Input needed"}</div>
      {questions.map((question) => (
        <fieldset className="question-field" key={question.id}>
          <legend>{question.header || question.question || question.id}</legend>
          {question.header && question.question ? (
            <p className="question-text">{question.question}</p>
          ) : null}
          {(question.choices || []).map((choice) => (
            <label className="question-choice" key={choice.id}>
              <input
                checked={answers[question.id]?.choice_id === choice.id}
                name={`q-${question.id}`}
                type="radio"
                onChange={() =>
                  setAnswers((current) =>
                    applyAnswerPatch(current, question.id, { choice_id: choice.id }),
                  )
                }
              />
              <span>{choice.label || choice.id}</span>
            </label>
          ))}
          {question.allow_freeform !== false ? (
            <>
              {(question.choices || []).length ? (
                <p className="question-hint">You can also add a note below</p>
              ) : null}
              <textarea
                rows={3}
                placeholder={(question.choices || []).length ? "Other answer" : "Answer"}
                value={answers[question.id]?.text || ""}
                onChange={(event) => {
                  // Read the value synchronously inside the handler body: React
                  // nulls the synthetic event's currentTarget after dispatch, but
                  // the setAnswers updater below runs in a later render phase —
                  // reading event.currentTarget there would be null, and .value
                  // would throw → a blank white page (no ErrorBoundary to catch it).
                  //
                  // Store the RAW value, never .trim() here: this is a controlled
                  // textarea, so trimming each keystroke strips a just-typed trailing
                  // space before the next character lands ("yes please" → "yesplease"),
                  // making multi-word freeform answers impossible. Trimming belongs at
                  // the readiness gate (questionSatisfied already trims) / submit, not
                  // on input.
                  const text = event.currentTarget.value;
                  setAnswers((current) =>
                    applyAnswerPatch(current, question.id, { text }),
                  );
                }}
              />
            </>
          ) : null}
        </fieldset>
      ))}
      <div className="approval-actions">
        <button
          className="approve-btn"
          disabled={!complete}
          type="button"
          onClick={() => onSubmit(pending.question_id, answers)}
        >
          Submit
        </button>
      </div>
    </section>
  );
}
// Mainstream-style "the assistant is composing" affordance: a pulsing brand
// avatar plus bouncing dots, anchored at the bottom-left where the next
// assistant bubble will materialise. An optional label carries lifecycle verbs.
function ResponseIndicator({ label }) {
  return (
    <div className="ks-typing" role="status" aria-live="polite">
      <span className="ks-typing__avatar" aria-hidden="true">
        <Bot size={ICON_SM} />
      </span>
      <span className="ks-typing__dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      {label ? <span className="ks-typing__label">{label}</span> : null}
    </div>
  );
}
function messageRole(turn, fullCache, textCache) {
  const hash = turn && turn.messagesRef;
  if (!hash) return null;
  const full = fullCache.get(hash);
  if (Array.isArray(full)) {
    if (!full.length) return "tool";
    const first = full.find((message) => message && message.role !== "system");
    return first?.role || "assistant";
  }
  const text = textCache.get(hash);
  if (Array.isArray(text)) {
    if (!text.length) return "tool";
    const first = text.find((message) => message && message.role !== "system");
    return first?.role || "assistant";
  }
  return null;
}

function userMessageText(turn, fullCache, textCache) {
  const hash = turn && turn.messagesRef;
  if (!hash) return "";
  const full = fullCache.get(hash);
  if (Array.isArray(full)) return canonicalProse(full, "user");
  const text = textCache.get(hash);
  if (Array.isArray(text)) return text.find((message) => message.role === "user")?.text || "";
  return "";
}

// extract the image blocks a user turn
// carries ([{hash, mediaType}]). Images appear only in the FULL cache: the
// /messages text endpoint drops non-text blocks (including image_block); only the
// full canonical message deref'd from /content carries an ImageBlock. So a
// text-only cache hit returns empty, and images appear once ensureMessageBodies
// fetches the full body (the same lazy-load rhythm as the bubble text).
function userMessageImageList(turn, fullCache, textCache) {
  const hash = turn && turn.messagesRef;
  if (!hash) return [];
  const full = fullCache.get(hash);
  if (Array.isArray(full)) return userMessageImages(full);
  return [];
}

function assistantMessageParts(turn, fullCache, textCache) {
  const hash = turn && turn.messagesRef;
  if (!hash) return [];
  const full = fullCache.get(hash);
  if (Array.isArray(full)) return canonicalAssistantParts(full);
  const text = textCache.get(hash);
  if (Array.isArray(text)) {
    return text
      .filter((message) => message.role === "assistant" && message.text)
      .map((message) => ({ type: "text", text: message.text }));
  }
  return [];
}

function canonicalBlocks(message) {
  if (!message) return [];
  if (Array.isArray(message.content)) return message.content;
  if (Array.isArray(message)) return message;
  return [];
}

function canonicalProse(messages, role) {
  const parts = [];
  for (const message of Array.isArray(messages) ? messages : []) {
    if (!message || message.role !== role) continue;
    for (const block of canonicalBlocks(message)) {
      if (block?.__canonical_tag__ === "text_block" && typeof block.text === "string") {
        parts.push(block.text);
      }
    }
  }
  return parts.join("\n\n").trim();
}

function canonicalAssistantParts(messages) {
  const parts = [];
  for (const message of Array.isArray(messages) ? messages : []) {
    if (!message || message.role !== "assistant") continue;
    for (const block of canonicalBlocks(message)) {
      if (block?.__canonical_tag__ === "thinking_block" && block.text) {
        parts.push({ type: "thinking", text: block.text });
      }
      if (block?.__canonical_tag__ === "text_block" && block.text) {
        parts.push({ type: "text", text: block.text });
      }
    }
  }
  return parts;
}

// Build a seq-sorted index of LLMResponseRecorded (seq, hash) pairs in one pass,
// so many "latest response before seq" lookups can binary-search a shared array
// instead of each re-scanning the full events list.
function buildResponseRefSeqIndex(events) {
  const index = [];
  for (const env of events) {
    if (!env || env.type !== "LLMResponseRecorded") continue;
    if (typeof env.seq !== "number") continue;
    const ref = env.payload?.response_ref;
    if (typeof ref?.hash !== "string") continue;
    index.push({ seq: env.seq, hash: ref.hash });
  }
  index.sort((a, b) => a.seq - b.seq);
  return index;
}

// O(log n) equivalent of responseRefHashForSeq against a buildResponseRefSeqIndex
// result: hash of the highest-seq response strictly before `seq`.
function responseRefHashForSeqIndex(index, seq) {
  if (typeof seq !== "number") return null;
  let lo = 0;
  let hi = index.length - 1;
  let best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (index[mid].seq < seq) {
      best = index[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best ? best.hash : null;
}

function toolArgHint(args) {
  if (!args || typeof args !== "object") return "";
  let value = null;
  for (const key of ["command", "file_path", "path", "pattern", "query", "url", "goal", "prompt"]) {
    if (typeof args[key] === "string" && args[key].trim()) {
      value = args[key];
      break;
    }
  }
  if (value == null) {
    for (const key of Object.keys(args)) {
      if (typeof args[key] === "string" && args[key].trim()) {
        value = args[key];
        break;
      }
    }
  }
  if (value == null) return "";
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length > 80 ? `${flat.slice(0, 80)}...` : flat;
}

export {
  ApprovalGroup,
  BackgroundJobsStrip,
  QuestionPrompt,
  ResponseIndicator,
  RunningStrip,
  TodoStrip,
  Transcript,
  commandPreview,
  friendlyAgentName,
};
