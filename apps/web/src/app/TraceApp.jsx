import {
  ArrowLeft,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Clipboard,
  Copy,
  FileText,
  Link2,
  Moon,
  RefreshCw,
  Search,
  Sun,
  Workflow,
  X,
} from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  KS_CATEGORIES,
  collectArtifacts,
  groupTurns,
  isNoiseRow,
  ksCategory,
  traceRows,
} from "../pages/trace/projection.js";
import {
  approxTokens,
  formatCost,
  formatDuration,
  formatTokens,
  safeJson,
  shortHash,
  shortId,
} from "../lib/format.js";
import { useThemeToggle } from "../lib/theme.js";
import { copyText } from "../lib/clipboard.js";
import { useTraceData } from "./trace-data.js";

// Lets any descendant open the shared preview modal (file artifacts and content
// refs) without threading a callback through every layer. taskId rides along so
// the modal can build the fetch URL.
const PreviewContext = createContext(null);
function usePreview() {
  return useContext(PreviewContext) || { openPreview: () => {}, taskId: null };
}

function TraceApp() {
  const trace = useTraceData();
  const { theme, toggleTheme } = useThemeToggle();
  const [filterText, setFilterText] = useState("");
  const [activeCategories, setActiveCategories] = useState(new Set());
  // When true the main timeline shows EVERY event including the lifecycle
  // plumbing (TaskCreated/ModelBound/TaskSnapshot/…); when false those rows are
  // hidden from the main list and surfaced only in the "raw events" drawer.
  const [showRawEvents, setShowRawEvents] = useState(false);
  const rows = useMemo(() => traceRows(trace.events), [trace.events]);
  const noiseRows = useMemo(() => rows.filter(isNoiseRow), [rows]);
  const matchedRows = rows.filter((row) =>
    rowMatchesFilter(row, filterText, activeCategories),
  );
  // The main timeline drops lifecycle noise unless "show all" is on; the drawer
  // always lists the dropped rows so nothing is truly hidden.
  const visibleRows = showRawEvents
    ? matchedRows
    : matchedRows.filter((row) => !isNoiseRow(row));
  const filtering = filterText.trim().length > 0 || activeCategories.size > 0;
  const summary = useMemo(
    () => sessionSummary(trace.events, trace.activeDetail),
    [trace.activeDetail, trace.events],
  );

  const [previewStack, setPreviewStack] = useState([]);
  const openPreview = useCallback((next) => setPreviewStack((stack) => [...stack, next]), []);
  const backPreview = useCallback(() => setPreviewStack((stack) => stack.slice(0, -1)), []);
  const closePreview = useCallback(() => setPreviewStack([]), []);

  return (
    <PreviewContext.Provider value={{ openPreview, taskId: trace.taskId }}>
    <div className="trace-page">
      <header className="trace-header">
        <div className="trace-titlebar">
          <div className="trace-title">
            <h1>Noeta trace</h1>
            <span className="ks-badge">{trace.taskId ? shortId(trace.taskId) : "no task"}</span>
            <span className="ks-badge">
              {trace.activeDetail?.status_text || trace.activeDetail?.status || "unknown"}
            </span>
          </div>
          <div className="connection-controls">
            <span className={`connection-status ${trace.connection.className}`}>
              {trace.connection.label}
            </span>
            <button
              className="soft-button"
              type="button"
              onClick={() => trace.startLiveTail(true)}
            >
              <RefreshCw size={14} />
              Reconnect
            </button>
            <button className="icon-button" type="button" onClick={toggleTheme}>
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
          </div>
        </div>
        <TraceSummary summary={summary} />
      </header>

      {trace.notice ? <div className={`banner ${trace.notice.kind}`}>{trace.notice.text}</div> : null}

      <main className="trace-workspace">
        <aside className="timeline-pane" aria-label="Event timeline">
          <TaskTree
            tasks={trace.tasks}
            currentId={trace.taskId}
            onNavigate={trace.navigateToTask}
          />
          <div className="pane-head">
            <h2>Timeline</h2>
            <span className="ks-badge">
              {visibleRows.length === rows.length
                ? `${rows.length} ${rows.length === 1 ? "event" : "events"}`
                : `${visibleRows.length} / ${rows.length}`}
            </span>
          </div>
          <div className="timeline-search">
            <Search size={15} />
            <input
              value={filterText}
              type="search"
              placeholder="Filter events"
              onChange={(event) => setFilterText(event.currentTarget.value)}
            />
          </div>
          <CategoryChips
            activeCategories={activeCategories}
            onChange={setActiveCategories}
          />
          <Timeline
            rows={visibleRows}
            sessionRows={rows}
            grouped={!filtering}
            selectedSeq={trace.selectedSeq}
            onSelect={trace.setSelectedSeq}
          />
          <RawEventsDrawer
            rows={noiseRows}
            sessionRows={rows}
            showInline={showRawEvents}
            onToggleInline={() => setShowRawEvents((prev) => !prev)}
            selectedSeq={trace.selectedSeq}
            onSelect={trace.setSelectedSeq}
          />
        </aside>

        <section className="detail-pane" aria-label="Event detail">
          <div className="pane-head">
            <h2>Event detail</h2>
          </div>
          <EventDetail
            event={trace.selectedEvent}
            events={trace.events}
            context={trace.activeContext}
            taskId={trace.taskId}
          />
        </section>

        <aside className="inspector-pane" aria-label="Session inspect">
          <Inspector
            context={trace.activeContext}
            detail={trace.activeDetail}
            events={trace.events}
            selectedSeq={trace.selectedSeq}
            taskId={trace.taskId}
            onSelect={trace.setSelectedSeq}
          />
        </aside>
      </main>
    </div>
      <PreviewModal
        stack={previewStack}
        taskId={trace.taskId}
        onBack={backPreview}
        onClose={closePreview}
      />
    </PreviewContext.Provider>
  );
}

function TraceSummary({ summary }) {
  // Cache hit rate is read tokens over total input — "how much of everything we
  // sent came from cache (billed cheap)". cache_write is a separate, costlier
  // tier (first-time cache fill), so it's shown apart, not folded into "hit".
  const cacheTotal = summary.cacheReadTokens + summary.cacheWriteTokens;
  const cacheHitRate =
    summary.inputTokens > 0 ? summary.cacheReadTokens / summary.inputTokens : 0;
  const stats = [
    { label: "model", value: summary.model, size: "wide" },
    { label: "events", value: String(summary.eventCount), size: "compact" },
    { label: "llm turns", value: String(summary.llmTurns), size: "compact" },
    {
      label: "tokens",
      value: `${formatTokens(summary.totalTokens)} (${formatTokens(summary.inputTokens)} in · ${formatTokens(summary.outputTokens)} out)`,
      size: "wide",
    },
  ];
  if (cacheTotal > 0) {
    stats.push({
      label: "cache",
      value: `${Math.round(cacheHitRate * 100)}% hit · ${formatTokens(summary.cacheReadTokens)} read · ${formatTokens(summary.cacheWriteTokens)} write`,
      size: "wide",
    });
  }
  stats.push(
    { label: "cost", value: formatCost(summary.costUsd), size: "compact" },
    {
      label: "duration",
      value: summary.duration == null ? "-" : formatDuration(summary.duration),
      size: "compact",
    },
  );
  return (
    <div className="trace-summary">
      {stats.map(({ label, value, size }) => (
        <div className={`summary-stat summary-stat--${size}`} key={label} title={`${label}: ${value}`}>
          <span className="summary-stat__label">{label}</span>
          <span className="summary-stat__value">{value}</span>
        </div>
      ))}
    </div>
  );
}

// Map the reserved orchestration agent name to a friendly label.
function friendlyAgentName(name) {
  if (name === "__workflow__") return "Workflow";
  return name || "agent";
}

function treeDotClass(status, closed) {
  const v = String(closed ? "closed" : status || "").toLowerCase();
  if (v.includes("run")) return "running";
  if (v.includes("fail") || v.includes("error") || v.includes("cancel")) return "failed";
  if (v.includes("term") || v.includes("complete") || v.includes("done")) return "completed";
  if (v.includes("wait") || v.includes("suspend")) return "waiting";
  return "";
}

// Build the task tree rooted at the open task's TOP-MOST ancestor, so the whole
// main → __workflow__ → workers hierarchy is visible from any node. Children are
// ordered by creation time (≈ spawn order). Returns a flat list of
// {task, depth} for rendering.
function buildTaskTree(tasks, currentId) {
  if (!Array.isArray(tasks) || !tasks.length || !currentId) return [];
  const byId = new Map(tasks.map((t) => [t.task_id, t]));
  if (!byId.has(currentId)) return [];
  const childrenOf = new Map();
  for (const t of tasks) {
    const p = t.parent_task_id || null;
    if (!childrenOf.has(p)) childrenOf.set(p, []);
    childrenOf.get(p).push(t);
  }
  for (const list of childrenOf.values()) {
    list.sort(
      (a, b) =>
        taskTreeOrder(a) - taskTreeOrder(b) ||
        String(a.task_id || "").localeCompare(String(b.task_id || "")),
    );
  }
  // Walk up to the root ancestor (guard against cycles).
  let root = currentId;
  const seen = new Set();
  while (!seen.has(root)) {
    seen.add(root);
    const parent = byId.get(root)?.parent_task_id;
    if (!parent || !byId.has(parent)) break;
    root = parent;
  }
  const flat = [];
  const walk = (id, depth) => {
    const task = byId.get(id);
    if (!task) return;
    flat.push({ task, depth });
    for (const child of childrenOf.get(id) || []) walk(child.task_id, depth + 1);
  };
  walk(root, 0);
  return flat;
}

function taskTreeOrder(task) {
  const created = Number(task?.created_event_time);
  if (Number.isFinite(created)) return created;
  const updated = Number(task?.last_event_time);
  return Number.isFinite(updated) ? updated : 0;
}

// The subtask tree: the trace is scoped to one task, but real work fans out into
// child tasks (a workflow + its workers). This panel makes that hierarchy
// visible and navigable — each node links to its own trace. Only shown when the
// open task actually has relatives (otherwise the flat timeline is the whole
// story). Other trace tools (LangSmith/Langfuse run trees, Jaeger span
// waterfalls) do the same: a hierarchy you drill into.
function TaskTree({ tasks, currentId, onNavigate }) {
  const flat = useMemo(() => buildTaskTree(tasks, currentId), [tasks, currentId]);
  if (flat.length <= 1) return null;
  return (
    <div className="trace-tree" aria-label="Task tree">
      <div className="pane-head">
        <h2>Tasks</h2>
        <span className="ks-badge">{flat.length}</span>
      </div>
      <div className="trace-tree__rows">
        {flat.map(({ task, depth }) => {
          const isCurrent = task.task_id === currentId;
          const isWorkflow = task.agent_name === "__workflow__";
          return (
            <a
              className={`trace-tree__row ${isCurrent ? "current" : ""}`}
              key={task.task_id}
              href={`/trace.html?task=${encodeURIComponent(task.task_id)}`}
              aria-current={isCurrent ? "page" : undefined}
              onClick={(event) => {
                if (
                  event.defaultPrevented ||
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                ) {
                  return;
                }
                event.preventDefault();
                onNavigate(task.task_id);
              }}
              style={{ paddingLeft: `${10 + depth * 16}px` }}
              title={task.task_id}
            >
              <span className={`trace-tree__dot ${treeDotClass(task.status, task.closed)}`} />
              {isWorkflow ? <Workflow size={13} /> : <Bot size={13} />}
              <span className="trace-tree__name">{friendlyAgentName(task.agent_name)}</span>
              <span className="trace-tree__meta">
                {task.closed ? "closed" : task.status} · seq{" "}
                {task.last_seq == null ? "?" : task.last_seq}
              </span>
            </a>
          );
        })}
      </div>
    </div>
  );
}

function CategoryChips({ activeCategories, onChange }) {
  return (
    <div className="category-chips">
      {KS_CATEGORIES.map((category) => (
        <button
          className={`cat-chip ${category.cls}`}
          key={category.cls}
          type="button"
          aria-pressed={activeCategories.has(category.cls)}
          onClick={() => {
            const next = new Set(activeCategories);
            if (next.has(category.cls)) next.delete(category.cls);
            else next.add(category.cls);
            onChange(next);
          }}
        >
          <span className="ks-cat-dot" />
          {category.label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Timeline (left pane) — flat when filtering, grouped into turns otherwise.
// ---------------------------------------------------------------------------

function Timeline({ rows, sessionRows, grouped, selectedSeq, onSelect }) {
  // Per-row clock + delta are session-wide aggregates that are identical to
  // recompute for every row. Build a seq -> { clock, delta } map ONCE per
  // render (one pass over sessionRows) instead of scanning the whole session
  // inside every TimelineRow, which made the timeline O(N^2) in event count.
  const clocks = useMemo(() => sessionClocks(sessionRows), [sessionRows]);
  if (!rows.length) return <div className="pane-empty">No events</div>;
  if (!grouped) {
    return (
      <ol className="timeline-list">
        {rows.map((row) => (
          <TimelineRow
            key={row.seq ?? `${row.type}-${row.occurredAt}`}
            row={row}
            clocks={clocks}
            selectedSeq={selectedSeq}
            onSelect={onSelect}
          />
        ))}
      </ol>
    );
  }
  const groups = groupTurns(rows);
  return (
    <ol className="timeline-list">
      {groups.map((group, index) => (
        <TimelineGroup
          key={group.kind === "turn" ? `turn-${group.headSeq}` : `setup-${index}`}
          group={group}
          clocks={clocks}
          selectedSeq={selectedSeq}
          onSelect={onSelect}
        />
      ))}
    </ol>
  );
}

function TimelineGroup({ group, clocks, selectedSeq, onSelect }) {
  const stats = summarizeTurnRows(group.rows);
  const hasSelected = group.rows.some((row) => row.seq === selectedSeq);
  const [open, setOpen] = useState(() => group.kind === "setup" || hasSelected);
  useEffect(() => {
    if (hasSelected && !open) setOpen(true);
  }, [selectedSeq, hasSelected]);
  // Clicking the turn header selects the turn's head request, which routes the
  // detail pane through TurnView — the whole-turn summary (prompt + output +
  // tool calls). The chevron beside it still toggles the per-event list, so the
  // fine-grained rows stay reachable. "setup" groups have no turn to select, so
  // their header only expands/collapses.
  const headSeq = group.kind === "turn" ? group.headSeq : null;
  const turnSelected = headSeq != null && headSeq === selectedSeq;
  return (
    <li className={`timeline-group ${hasSelected ? "has-active" : ""}`}>
      <div className={`turn-group-head ${turnSelected ? "turn-selected" : ""}`}>
        <button
          className="turn-group-toggle"
          type="button"
          aria-expanded={open}
          aria-label={open ? "Collapse turn events" : "Expand turn events"}
          onClick={() => setOpen((prev) => !prev)}
        >
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
        <button
          className="turn-group-select"
          type="button"
          onClick={() => (headSeq != null ? onSelect(headSeq) : setOpen((prev) => !prev))}
        >
          <span className="turn-group-title">
            {group.kind === "turn" ? `Turn ${group.turnNo}` : "setup"}
          </span>
          {stats ? <span className="turn-group-meta">{stats}</span> : null}
        </button>
      </div>
      {open ? (
        <ol className="timeline-group-rows">
          {group.rows.map((row) => (
            <TimelineRow
              key={row.seq ?? `${row.type}-${row.occurredAt}`}
              row={row}
              clocks={clocks}
              selectedSeq={selectedSeq}
              onSelect={onSelect}
            />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

// Collapsible drawer for the lifecycle plumbing kept OUT of the main timeline
// (TaskCreated / ModelBound / TaskSnapshot / …). Default-collapsed. The toggle
// also flips whether those rows render inline in the main timeline, so you can
// either drill them here or fold them back into the chronological view.
function RawEventsDrawer({ rows, sessionRows, showInline, onToggleInline, selectedSeq, onSelect }) {
  const [open, setOpen] = useState(false);
  const clocks = useMemo(() => sessionClocks(sessionRows), [sessionRows]);
  if (!rows.length) return null;
  return (
    <section className="raw-events-drawer">
      <div className="raw-events-head">
        <button
          className="raw-events-toggle"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
        >
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="raw-events-title">raw events</span>
          <span className="ks-badge">{rows.length}</span>
        </button>
        <button
          className={`raw-events-inline ${showInline ? "on" : ""}`}
          type="button"
          aria-pressed={showInline}
          title="Show lifecycle events inline in the main timeline"
          onClick={onToggleInline}
        >
          {showInline ? "hide from timeline" : "show all in timeline"}
        </button>
      </div>
      {open ? (
        <ol className="timeline-list raw-events-list">
          {rows.map((row) => (
            <TimelineRow
              key={row.seq ?? `${row.type}-${row.occurredAt}`}
              row={row}
              clocks={clocks}
              selectedSeq={selectedSeq}
              onSelect={onSelect}
            />
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function TimelineRow({ row, clocks, selectedSeq, onSelect }) {
  const clock = clocks.get(row.seq) || EMPTY_CLOCK;
  return (
    <li>
      <button
        className={`timeline-row ${row.category} ${row.seq === selectedSeq ? "active" : ""}`}
        type="button"
        onClick={() => onSelect(row.seq)}
      >
        <span className="trace-head">
          <span className="trace-type">{row.type}</span>
          <span className="trace-time">{clock.clock}</span>
          <span className="trace-seq">{row.seq == null ? "?" : `#${row.seq}`}</span>
        </span>
        <span className="trace-meta">
          {[row.summary, clock.delta].filter(Boolean).join(" · ")}
        </span>
      </button>
    </li>
  );
}

const EMPTY_CLOCK = { clock: "", delta: "" };

// Compact one-line summary of a turn group from its rows' payloads.
// Real per-call token usage lives in LLMRequestFinished.usage — the
// provider-reported counts. LLMRequestStarted.input_tokens is a hardcoded-0
// placeholder (emitted before the provider returns usage), so never read it.
// `input` sums uncached + both cache tiers: cache reads/writes are still input
// tokens, just billed at different rates. Usage carries no canonical tag, so it
// arrives as a plain { uncached, cache_read, cache_write, output, reasoning_tokens } dict.
function usageTokens(usage) {
  if (!usage || typeof usage !== "object") return null;
  const uncached = usage.uncached || 0;
  const cacheRead = usage.cache_read || 0;
  const cacheWrite = usage.cache_write || 0;
  return {
    input: uncached + cacheRead + cacheWrite,
    output: usage.output || 0,
    uncached,
    cacheRead,
    cacheWrite,
    reasoning: usage.reasoning_tokens || 0,
  };
}

function summarizeTurnRows(rows) {
  let model = null;
  let usage = null;
  let outTokFallback = null;
  let cost = null;
  let latency = null;
  for (const row of rows || []) {
    const payload = row.payload || {};
    if (row.type === "LLMRequestStarted") {
      if (payload.model) model = payload.model;
    } else if (row.type === "LLMResponseRecorded") {
      if (typeof payload.output_tokens === "number") outTokFallback = payload.output_tokens;
    } else if (row.type === "LLMRequestFinished") {
      if (payload.usage) usage = usageTokens(payload.usage);
      if (typeof payload.cost_usd === "number") cost = payload.cost_usd;
      if (typeof payload.latency_ms === "number") latency = payload.latency_ms;
    }
  }
  // Prefer the finished-event usage (real input→output). Before the turn
  // finishes we only know output, so show "…→N" rather than a misleading 0→N.
  const tokens = usage
    ? `${formatTokens(usage.input)}→${formatTokens(usage.output)} tok`
    : outTokFallback != null
      ? `…→${formatTokens(outTokFallback)} tok`
      : null;
  return [
    model,
    tokens,
    cost != null ? formatCost(cost) : null,
    latency != null ? `${Math.round(latency)}ms` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

// ---------------------------------------------------------------------------
// Event detail (middle pane) — LangSmith-style region view for LLM turns.
// ---------------------------------------------------------------------------

function EventDetail({ event, events, context, taskId }) {
  if (!event) return <div className="pane-empty">Select an event from the timeline</div>;
  if (/^LLM/.test(event.type || "")) {
    return <TurnView selected={event} events={events} context={context} taskId={taskId} />;
  }
  if (event.type === "ContextPlanComposed" && refHash(event.payload?.plan_ref)) {
    return (
      <div className="event-detail">
        <div className={`event-head ${ksCategory(event.type)}`}>
          <span className="trace-type">{event.type}</span>
          <span className="trace-seq">seq {event.seq ?? "?"}</span>
        </div>
        <DerefedBody hash={refHash(event.payload.plan_ref)} renderValue={renderContextPlanBody} taskId={taskId} />
        <RawRegion event={event} taskId={taskId} />
      </div>
    );
  }
  return <PlainEvent event={event} taskId={taskId} />;
}

// One LLM turn rendered as stacked regions: header badges, the prompt the model
// saw (system / tools / conversation), why that context was chosen, and the
// model's output — input and output on one screen.
function TurnView({ selected, events, context, taskId }) {
  const callId = selected?.payload?.call_id || null;
  let request = null;
  let response = null;
  let finished = null;
  for (const event of events) {
    if (!event || !/^LLM/.test(event.type || "")) continue;
    const match = callId ? event.payload?.call_id === callId : event === selected;
    if (!match) continue;
    if (event.type === "LLMRequestStarted") request = event;
    else if (event.type === "LLMResponseRecorded") response = event;
    else if (event.type === "LLMRequestFinished") finished = event;
  }
  const requestHash = refHash(request?.payload?.request_ref);
  const responseHash = refHash(response?.payload?.response_ref);
  const { plan, selection } = turnProvenance(context, request?.seq ?? selected?.seq, callId);
  const toolEvents = turnToolEvents(events, request?.seq ?? selected?.seq);
  return (
    <div className="event-detail turn-view">
      <TurnHeader request={request} response={response} finished={finished} selected={selected} />
      {requestHash ? (
        <DerefedBody hash={requestHash} renderValue={renderTurnRequest} taskId={taskId} />
      ) : null}
      {plan || selection ? (
        <ProvenanceRegion plan={plan} selection={selection} events={events} requestSeq={request?.seq ?? selected?.seq} />
      ) : null}
      {responseHash ? (
        <DerefedBody hash={responseHash} renderValue={renderTurnOutput} taskId={taskId} />
      ) : null}
      <TurnToolsRegion toolEvents={toolEvents} />
      <RawRegion event={selected} taskId={taskId} />
    </div>
  );
}

// The tool calls dispatched AFTER this turn's request and BEFORE the next one —
// i.e. the actions the model's output drove this turn, paired with their
// recorded results. The request/response bodies show the prompt and the
// assistant text; this region shows what actually ran in between.
function turnToolEvents(events, requestSeq) {
  if (typeof requestSeq !== "number") return [];
  let nextRequestSeq = Infinity;
  for (const event of events) {
    if (event?.type === "LLMRequestStarted" && typeof event.seq === "number" && event.seq > requestSeq) {
      nextRequestSeq = Math.min(nextRequestSeq, event.seq);
    }
  }
  const starts = new Map();
  const out = [];
  for (const event of events) {
    if (typeof event?.seq !== "number") continue;
    if (event.seq <= requestSeq || event.seq >= nextRequestSeq) continue;
    if (event.type === "ToolCallStarted") {
      const callId = event.payload?.call_id || null;
      const entry = { callId, started: event, result: null };
      if (callId) starts.set(callId, entry);
      out.push(entry);
    } else if (event.type === "ToolResultRecorded") {
      const callId = event.payload?.call_id || null;
      const existing = callId ? starts.get(callId) : null;
      if (existing) existing.result = event;
      else out.push({ callId, started: null, result: event });
    }
  }
  return out;
}

function TurnToolsRegion({ toolEvents }) {
  if (!toolEvents.length) return null;
  return (
    <Region title={`Tool calls · ${toolEvents.length}`} tone="tools" defaultOpen>
      <div className="turn-tool-list">
        {toolEvents.map((entry, index) => (
          <TurnToolCall key={entry.callId || index} entry={entry} />
        ))}
      </div>
    </Region>
  );
}

function TurnToolCall({ entry }) {
  const sp = entry.started?.payload || {};
  const rp = entry.result?.payload || {};
  const name = sp.tool_name || "tool";
  const ok = rp.success !== false;
  const args = sp.arguments ?? sp.input ?? null;
  return (
    <div className={`turn-tool ${entry.result && !ok ? "turn-tool-err" : ""}`}>
      <div className="turn-tool-head">
        <span className="turn-tool-name">{name}</span>
        {entry.callId ? <span className="turn-tool-id">{shortId(entry.callId)}</span> : null}
        {entry.result ? (
          <span className={`turn-tool-status ${ok ? "ok" : "err"}`}>{ok ? "ok" : "error"}</span>
        ) : (
          <span className="turn-tool-status pending">pending</span>
        )}
      </div>
      {args != null ? (
        <ExpandableText text={typeof args === "string" ? args : safeJson(args)} mono />
      ) : null}
      {entry.result ? (
        <div className="turn-tool-result">
          {rp.summary ? <div className="turn-tool-summary">{rp.summary}</div> : null}
          <RefChipRow payload={rp} />
        </div>
      ) : null}
    </div>
  );
}

function TurnHeader({ request, response, finished, selected }) {
  const rp = request?.payload || {};
  const sp = response?.payload || {};
  const fp = finished?.payload || {};
  const model = rp.model || sp.model || fp.model || null;
  const usage = usageTokens(fp.usage);
  const inTok = usage ? usage.input : null;
  const outTok = usage
    ? usage.output
    : typeof sp.output_tokens === "number"
      ? sp.output_tokens
      : null;
  const cached = usage && (usage.cacheRead || usage.cacheWrite) ? usage.cacheRead + usage.cacheWrite : null;
  const cost = typeof fp.cost_usd === "number" ? fp.cost_usd : null;
  const latency = typeof fp.latency_ms === "number" ? fp.latency_ms : null;
  const stop = fp.stop_reason || sp.stop_reason || null;
  const badges = [
    model ? model : null,
    inTok != null ? `${formatTokens(inTok)} in` : null,
    cached != null ? `${formatTokens(cached)} cached` : null,
    outTok != null ? `${formatTokens(outTok)} out` : null,
    cost != null ? formatCost(cost) : null,
    latency != null ? `${Math.round(latency)}ms` : null,
    stop ? stop : null,
  ].filter(Boolean);
  return (
    <div className="turn-header">
      <div className={`event-head ${ksCategory(selected.type || "")}`}>
        <span className="trace-type">{selected.type}</span>
        <span className="trace-seq">seq {selected.seq ?? "?"}</span>
      </div>
      {badges.length ? (
        <div className="detail-badges">
          {badges.map((value, index) => (
            <span className="ks-badge" key={`${value}-${index}`}>{value}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// renderValue for the request body → the prompt split into regions.
function renderTurnRequest(req) {
  const system = req?.system;
  const systemText = messageText(system);
  const messages = Array.isArray(req?.messages) ? req.messages : [];
  const tools = Array.isArray(req?.tools) ? req.tools : [];
  const conversationText = messages.map(messageText).join("\n");
  return (
    <>
      {system ? (
        <Region
          title="System prompt"
          tone="system"
          badge={tokenBadge(systemText)}
          copyText={systemText}
          defaultOpen={false}
        >
          <ExpandableText text={systemText} />
        </Region>
      ) : null}
      <ToolsRegion tools={tools} />
      <Region
        title={`Conversation · ${messages.length} ${messages.length === 1 ? "message" : "messages"}`}
        tone="conversation"
        badge={tokenBadge(conversationText)}
      >
        <div className="msg-list">
          {messages.length ? (
            messages.map((message, index) => (
              <MessageCard key={index} message={message} role={message?.role || "user"} />
            ))
          ) : (
            <div className="pane-empty">(no messages)</div>
          )}
        </div>
      </Region>
      <RequestParamsRegion request={req || {}} />
    </>
  );
}

// renderValue for the response body → the assistant output region.
function renderTurnOutput(resp) {
  const text = messageText(resp);
  return (
    <Region title="Output" tone="output" badge={tokenBadge(text)} copyText={text}>
      <div className="msg-list">
        <MessageCard message={resp || {}} role="assistant" defaultOpen />
      </div>
      <dl className="detail-table req-meta">
        <div><dt>stop_reason</dt><dd>{resp?.stop_reason || "-"}</dd></div>
        <div><dt>output_tokens</dt><dd>{resp?.usage?.output ?? "-"}</dd></div>
      </dl>
    </Region>
  );
}

function ToolsRegion({ tools }) {
  if (!tools.length) return null;
  const toolsText = tools.map((tool) => safeJson(tool)).join("\n");
  return (
    <Region
      title={`Tools · ${tools.length}`}
      tone="tools"
      badge={tokenBadge(toolsText)}
      defaultOpen={false}
    >
      <div className="tool-defs">
        {tools.map((tool, index) => (
          <ToolDef key={index} tool={tool} />
        ))}
      </div>
    </Region>
  );
}

function ToolDef({ tool }) {
  const { openPreview } = usePreview();
  const name = tool?.name || tool?.function?.name || "tool";
  const desc = tool?.description || tool?.function?.description || "";
  const schema = tool?.input_schema || tool?.parameters || tool?.function?.parameters || null;
  return (
    <div className="tool-def">
      <div className="tool-def-name">{name}</div>
      {desc ? <div className="tool-def-desc">{desc}</div> : null}
      {schema ? (
        <button
          className="ref-chip"
          type="button"
          onClick={() => openPreview({ title: `${name} · schema`, text: safeJson(schema) })}
        >
          <Link2 size={12} />
          <span className="ref-chip-label">schema</span>
        </button>
      ) : null}
    </div>
  );
}

function ProvenanceRegion({ plan, selection, events, requestSeq }) {
  const skills = Array.isArray(plan?.selected_skills) ? plan.selected_skills : [];
  const resources = Array.isArray(plan?.retrieved_resources) ? plan.retrieved_resources : [];
  // The plan's content-addressed ref lists are the source of truth for what the
  // composer kept / pruned this turn. The MessageSelection counts only exist
  // when a count-based truncation was recorded — rare since context compaction made the
  // tail-window guard default-off — and otherwise degrade to 0/""; so they are
  // shown only as an extra `strategy` row when one is actually present, never as
  // the primary figure (which previously made every turn read as selected 0).
  const keptCount = Array.isArray(plan?.selected_messages)
    ? plan.selected_messages.length
    : null;
  const clearedCount = Array.isArray(plan?.dropped_messages)
    ? plan.dropped_messages.length
    : 0;
  const strategy = selection?.strategy || "";
  const residents = activeResidents(events, requestSeq);
  return (
    <Region title="Context provenance" tone="provenance" defaultOpen={false}>
      <dl className="detail-table">
        <div><dt>composer</dt><dd>{plan?.composer_version || "-"}</dd></div>
        <div><dt>kept msgs</dt><dd>{keptCount ?? "-"}</dd></div>
        {/* Only surface micro-compaction when it actually pruned something — a
            "none"/0 row used to read as if compaction was running every turn. */}
        {clearedCount > 0 ? (
          <div>
            <dt>micro-compaction</dt>
            <dd>{`cleared ${clearedCount} tool outputs (deref-able)`}</dd>
          </div>
        ) : null}
        {strategy ? (
          <div>
            <dt>strategy</dt>
            <dd>
              {strategy} · selected {selection?.selected ?? "-"} · dropped{" "}
              {selection?.dropped ?? "-"}
            </dd>
          </div>
        ) : null}
      </dl>
      {residents.length ? (
        <div className="prov-block">
          <div className="prov-label">active residents</div>
          <div className="chip-row">
            {residents.map((res) => (
              <span className="chip" key={`${res.kind}:${res.name}`} title={res.policy ? `policy: ${res.policy}` : undefined}>
                {res.kind}:{res.name}
              </span>
            ))}
          </div>
        </div>
      ) : null}
      {skills.length ? (
        <div className="prov-block">
          <div className="prov-label">skills</div>
          <div className="chip-row">
            {skills.map((skill) => (
              <span className="chip" key={skill}>{skill}</span>
            ))}
          </div>
        </div>
      ) : null}
      {resources.length ? (
        <div className="prov-block">
          <div className="prov-label">retrieved resources</div>
          {resources.map((resource, index) => (
            <div className="context-meta" key={index}>
              {[resource?.skill, resource?.relpath].filter(Boolean).join("/") ||
                resource?.reason ||
                "resource"}
              {resource?.reason && (resource?.skill || resource?.relpath) ? ` · ${resource.reason}` : ""}
              {resource?.bytes != null ? ` · ${resource.bytes}b` : ""}
              {resource?.hash ? ` · ${shortHash(resource.hash)}` : ""}
            </div>
          ))}
        </div>
      ) : null}
      <div className="ref-note">
        Provenance / selection info. The per-segment text of the system prompt's
        internal sub-sections (stable / semi / dynamic) is not stored separately;
        see the System prompt region above for the full system prompt.
      </div>
    </Region>
  );
}

// ---------------------------------------------------------------------------
// Messages & blocks
// ---------------------------------------------------------------------------

function MessageCard({ message, role, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const blocks = Array.isArray(message?.content) ? message.content : [];
  const text = messageText(message);
  return (
    <details
      className={`msg msg-${role}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="msg-head">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span className={`msg-role role-${role}`}>{role}</span>
        <span className="msg-preview">{previewLine(text)}</span>
        <span className="msg-tok">{tokenBadge(text)}</span>
      </summary>
      <div className="msg-blocks">
        {blocks.length ? (
          blocks.map((block, index) => <CanonicalBlock block={block} key={index} />)
        ) : (
          <div className="msg-empty">(no content)</div>
        )}
      </div>
    </details>
  );
}

function CanonicalBlock({ block }) {
  const tag = block?.__canonical_tag__;
  if (tag === "text_block") return <ExpandableText text={block.text || ""} />;
  if (tag === "thinking_block") {
    return (
      <details className="msg-thinking">
        <summary>thinking</summary>
        <ExpandableText text={block.text || ""} />
      </details>
    );
  }
  if (tag === "tool_use_block") {
    return (
      <div className="msg-tool msg-tool-use">
        <div className="msg-tool-head">
          → {block.tool_name || "tool"}{block.call_id ? ` · ${shortId(block.call_id)}` : ""}
        </div>
        <ExpandableText text={safeJson(block.arguments || {})} mono />
      </div>
    );
  }
  if (tag === "tool_result_block") {
    const out = typeof block.output === "string" ? block.output : safeJson(block.output);
    return (
      <div className={`msg-tool msg-tool-result ${block.success === false ? "msg-tool-err" : ""}`}>
        <div className="msg-tool-head">
          ← result{block.call_id ? ` · ${shortId(block.call_id)}` : ""}
          {block.success === false ? " · error" : ""}
        </div>
        <RefChipRow payload={block} />
        <ExpandableText text={out} mono />
      </div>
    );
  }
  if (tag === "image_block") {
    return (
      <div className="msg-tool">
        <div className="msg-tool-head">image</div>
        <RefChipRow payload={block} />
        <pre className="msg-tool-args">{safeJson(block.source || block)}</pre>
      </div>
    );
  }
  return (
    <div className="msg-tool">
      <RefChipRow payload={block} />
      <pre className="msg-tool-args">{safeJson(block)}</pre>
    </div>
  );
}

const TEXT_CAP = 1600;

function ExpandableText({ text, mono, cap = TEXT_CAP, diff = false }) {
  const [open, setOpen] = useState(false);
  const value = typeof text === "string" ? text : String(text ?? "");
  const long = value.length > cap;
  const shown = !long || open ? value : value.slice(0, cap);
  return (
    <div className="expandable">
      <pre className={mono ? "msg-text msg-text-mono" : "msg-text"}>
        {diff ? renderPreviewText(shown) : shown}
        {long && !open ? " …" : ""}
      </pre>
      {long ? (
        <button className="show-more" type="button" onClick={() => setOpen((prev) => !prev)}>
          {open ? "show less" : `show more · ${value.length.toLocaleString()} chars`}
        </button>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Generic region + content deref primitives
// ---------------------------------------------------------------------------

function Region({ title, badge, tone, copyText, defaultOpen = true, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={`region ${tone ? `region-${tone}` : ""}`}>
      <div className="region-head">
        <button
          className="region-toggle"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span className="region-title">{title}</span>
        </button>
        <div className="region-actions">
          {badge ? <span className="region-badge">{badge}</span> : null}
          {copyText != null ? <CopyButton text={copyText} /> : null}
        </div>
      </div>
      {open ? <div className="region-body">{children}</div> : null}
    </section>
  );
}

function CopyButton({ text }) {
  // B4 — TraceApp has no toast surface, so feed back inline: the icon flips to a
  // check on success / the title to "Copy failed" on failure (non-secure context
  // where both clipboard paths no-op), instead of silently doing nothing.
  const [state, setState] = useState("idle"); // idle | ok | fail
  useEffect(() => {
    if (state === "idle") return undefined;
    const t = window.setTimeout(() => setState("idle"), 1200);
    return () => window.clearTimeout(t);
  }, [state]);
  return (
    <button
      className="region-copy"
      type="button"
      title={state === "fail" ? "Copy failed — select manually" : state === "ok" ? "Copied" : "Copy"}
      onClick={async (event) => {
        event.stopPropagation();
        setState((await copyText(text)) ? "ok" : "fail");
      }}
    >
      {state === "ok" ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}

function RequestParamsRegion({ request }) {
  const tools = Array.isArray(request.tools) ? request.tools : [];
  const toolNames = tools
    .map((tool) => tool?.name || tool?.function?.name || null)
    .filter(Boolean);
  return (
    <Region title="Request params" defaultOpen={false}>
      <dl className="detail-table req-meta">
        <div><dt>model</dt><dd>{request.model || "-"}</dd></div>
        <div><dt>tools</dt><dd>{toolNames.length ? toolNames.join(", ") : tools.length || "-"}</dd></div>
        <div><dt>max_tokens</dt><dd>{request.max_tokens ?? "-"}</dd></div>
        <div><dt>temperature</dt><dd>{request.temperature ?? "-"}</dd></div>
      </dl>
    </Region>
  );
}

const contentBodyCache = new Map();

// Shared content-deref hook backing both DerefedBody (whole-body renders) and
// the inline RefChip expander. Fetches the global, content-addressed
// `GET /content/{hash}` once per hash (the new protocol's RAW-bytes blob route —
// no task scope, media type in the Content-Type header), caches it in the
// module-level contentBodyCache (so re-selecting a turn / re-expanding a chip is
// instant), and parses the body text. `enabled` lets a caller defer the fetch
// until the chip is expanded. Returns the cache entry:
// { state: "loading" | "ok" | "error", value?, error? }.
function useContentBody(hash, taskId, enabled = true) {
  const [, bumpRender] = useState(0);
  useEffect(() => {
    if (!enabled || !hash) return undefined;
    const cached = contentBodyCache.get(hash);
    if (cached && (cached.state === "ok" || cached.state === "error")) return undefined;
    contentBodyCache.set(hash, { state: "loading" });
    let cancelled = false;
    fetch(`/content/${encodeURIComponent(hash)}`)
      .then(async (res) => {
        let next;
        if (res.ok) {
          const text = await res.text();
          next = {
            state: "ok",
            value: parseJsonMaybe(text),
            text,
            mediaType: res.headers.get("Content-Type") || null,
          };
        } else {
          next = { state: "error", error: `HTTP ${res.status}` };
        }
        contentBodyCache.set(hash, next);
        if (!cancelled) bumpRender((n) => n + 1);
      })
      .catch((error) => {
        const next = { state: "error", error: error.message || "fetch failed" };
        contentBodyCache.set(hash, next);
        if (!cancelled) bumpRender((n) => n + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [hash, taskId, enabled]);
  return enabled ? contentBodyCache.get(hash) : null;
}

// Parse content text as JSON, falling back to the raw string when it isn't
// JSON (diffs / plain text). Always succeeds — value is the parsed object or the
// raw string.
function parseJsonMaybe(text) {
  try {
    return JSON.parse(text);
  } catch (error) {
    return text;
  }
}

function DerefedBody({ hash, renderValue, taskId }) {
  const current = useContentBody(hash, taskId);
  if (!current || current.state === "loading") return <div className="pane-empty">loading content...</div>;
  if (current.state === "error") return <div className="pane-empty">could not load content: {current.error}</div>;
  return <div className="deref-body">{renderValue(current.value, taskId)}</div>;
}

function RefChipRow({ payload }) {
  const refs = collectPayloadRefs(payload);
  if (!refs.length) return null;
  return (
    <div className="ref-chip-row">
      {refs.map((ref) => (
        <RefChip key={`${ref.label}-${ref.hash}`} refInfo={ref} />
      ))}
    </div>
  );
}

function RawRegion({ event }) {
  const payload = event?.payload || {};
  return (
    <Region title="Raw event payload" defaultOpen={false}>
      <RefChipRow payload={payload} />
      <CopyPre className="trace-payload" text={safeJson(payload)} />
    </Region>
  );
}

function PlainEvent({ event }) {
  const payload = event?.payload || {};
  return (
    <div className="event-detail">
      <div className={`event-head ${ksCategory(event.type || "")}`}>
        <span className="trace-type">{event.type}</span>
        <span className="trace-seq">seq {event.seq ?? "?"}</span>
      </div>
      <RefChipRow payload={payload} />
      <CopyPre className="trace-payload" text={safeJson(payload)} />
    </div>
  );
}

// Bytes a deref body should keep folded even when its chip is expanded — past
// this the body would dominate the pane, so it stays one click further behind a
// "show more" inside the expander. Tuned to ~16 KB.
const REF_INLINE_CAP = 16 * 1024;

function formatRefSize(size) {
  if (typeof size !== "number" || !Number.isFinite(size) || size < 0) return null;
  if (size < 1024) return `${size} B`;
  const kb = size / 1024;
  if (kb < 1024) return `${kb < 10 ? Math.round(kb * 10) / 10 : Math.round(kb)} KB`;
  return `${Math.round((kb / 1024) * 10) / 10} MB`;
}

// A content_ref rendered inline rather than behind a modal: collapsed it is a
// one-line chip (label · media_type · size · hash); clicking expands the
// dereferenced body IN PLACE (deref on first expand, cached after). Nested
// content_refs found inside the fetched body render as their own RefChips, so
// the recursive drill-in the old modal had is preserved — just inline. `depth`
// guards against a ref cycle blowing the stack.
function RefChip({ refInfo, depth = 0 }) {
  const { taskId } = usePreview();
  const [open, setOpen] = useState(false);
  const current = useContentBody(refInfo.hash, taskId, open);
  const manifest = refIsManifest(refInfo.label);
  const sizeLabel = formatRefSize(refInfo.size);
  const meta = [refInfo.mediaType, sizeLabel].filter(Boolean).join(" · ");
  return (
    <div className={`ref-inline ${open ? "open" : ""}`}>
      <button
        className="ref-chip ref-chip-inline"
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Link2 size={12} />
        <span className="ref-chip-label">{refInfo.label}</span>
        {meta ? <span className="ref-chip-meta">{meta}</span> : null}
        <span className="ref-chip-hash">{shortHash(refInfo.hash)}</span>
      </button>
      {open ? (
        <div className="ref-inline-body">
          {manifest ? (
            <div className="ref-note">
              Provenance manifest — open the matching LLM request to read what the model saw.
            </div>
          ) : null}
          <RefBody current={current} depth={depth} taskId={taskId} />
        </div>
      ) : null}
    </div>
  );
}

// The dereferenced body of one expanded RefChip: loading / error / content. The
// content is shown as colourised text (diff-aware) inside an ExpandableText so a
// large body stays folded to REF_INLINE_CAP until a further click. Any nested
// content_refs in the body are listed as their own RefChips (recursive deref).
function RefBody({ current, depth, taskId }) {
  if (!current || current.state === "loading") return <div className="pane-empty">loading…</div>;
  if (current.state === "error") return <div className="pane-empty">could not load: {current.error}</div>;
  const value = current.value;
  const text =
    typeof value === "string" ? value : current.text != null ? current.text : safeJson(value);
  const nestedRefs =
    depth < 6 && value && typeof value === "object" ? collectPayloadRefs(value) : [];
  return (
    <>
      <ExpandableText text={text} mono cap={REF_INLINE_CAP} diff />
      {nestedRefs.length ? (
        <div className="ref-inline-nested">
          <span className="ref-inline-nested-label">nested · {nestedRefs.length}</span>
          <div className="ref-chip-row">
            {nestedRefs.map((ref) => (
              <RefChip key={`${ref.label}-${ref.hash}`} refInfo={ref} depth={depth + 1} />
            ))}
          </div>
        </div>
      ) : null}
    </>
  );
}

// A file artifact shown as a compact row; clicking previews it in the modal
// (image inline, text/diff rendered) rather than navigating to the raw URL.
function ArtifactRow({ artifact }) {
  const { openPreview } = usePreview();
  const name = artifact.toolName ? `${artifact.toolName} · ${artifact.label}` : artifact.label;
  const meta = [artifact.mediaType, artifact.seq != null ? `seq ${artifact.seq}` : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <button
      className="artifact-row"
      type="button"
      onClick={() =>
        openPreview({ kind: "artifact", title: name, hash: artifact.hash, mediaType: artifact.mediaType })
      }
    >
      <FileText size={13} />
      <span className="artifact-name">{name}</span>
      {meta ? <span className="artifact-row-meta">{meta}</span> : null}
      <span className="artifact-hash">{shortHash(artifact.hash)}</span>
    </button>
  );
}

function isImageMedia(mediaType) {
  return /^image\//.test(mediaType || "");
}

// Parse JSON content so we can both pretty-print it and walk it for nested
// content refs; returns null for non-JSON (raw text / diffs).
function parseMaybe(text) {
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" ? value : null;
  } catch (error) {
    return null;
  }
}

function looksLikeDiff(text) {
  return /^(diff --git |@@ |--- |\+\+\+ )/m.test(text || "");
}

// Render plain text, colourising it as a unified diff when it looks like one.
function renderPreviewText(text) {
  const value = String(text ?? "");
  if (!looksLikeDiff(value)) return value;
  return value
    .split("\n")
    .slice(0, 5000)
    .map((line, index) => {
      const cls =
        line.startsWith("+++") || line.startsWith("---")
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

// One overlay for every preview: file artifacts (image or text/diff) and content
// refs. The previews form a stack — opening a ref nested inside the current body
// pushes a new frame, so you can drill in arbitrarily deep and step back out.
// Closes on backdrop click or the X; Escape steps back one level (or closes at
// the root). Inline `text` skips the fetch (used for schemas); otherwise the
// body is loaded from the content or artifact endpoint on open.
function PreviewModal({ stack, taskId, onBack, onClose }) {
  const depth = stack.length;
  const preview = depth ? stack[depth - 1] : null;
  const [state, setState] = useState({ status: "idle" });

  useEffect(() => {
    if (!preview) return undefined;
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      if (depth > 1) onBack();
      else onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [preview, depth, onBack, onClose]);

  useEffect(() => {
    if (!preview) {
      setState({ status: "idle" });
      return undefined;
    }
    if (typeof preview.text === "string") {
      setState({ status: "ok", text: preview.text, data: parseMaybe(preview.text) });
      return undefined;
    }
    if (isImageMedia(preview.mediaType)) {
      setState({ status: "image" });
      return undefined;
    }
    let cancelled = false;
    setState({ status: "loading" });
    // New protocol: artifacts and content alike deref from the global,
    // content-addressed GET /content/{hash} (RAW bytes, no task scope).
    const load = fetch(`/content/${encodeURIComponent(preview.hash)}`).then((resp) =>
      resp.ok ? resp.text() : `error: HTTP ${resp.status}`,
    );
    load
      .then((text) => {
        if (cancelled) return;
        const data = parseMaybe(text);
        setState({ status: "ok", text: data ? safeJson(data) : text, data });
      })
      .catch((error) => {
        if (!cancelled) {
          setState({ status: "ok", text: `error: ${error.message || "fetch failed"}`, data: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [preview, taskId]);

  if (!preview) return null;
  const imageUrl =
    state.status === "image" && preview.hash
      ? `/content/${encodeURIComponent(preview.hash)}`
      : null;
  // Refs found INSIDE the current body — each opens a nested preview.
  const nestedRefs = state.status === "ok" && state.data ? collectPayloadRefs(state.data) : [];
  return createPortal(
    <div className="preview-overlay" role="presentation" onClick={onClose}>
      <div
        className="preview-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={preview.title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="preview-head">
          {depth > 1 ? (
            <button className="preview-close" type="button" title="Back" onClick={onBack}>
              <ArrowLeft size={15} />
            </button>
          ) : (
            <FileText size={14} />
          )}
          <span className="preview-title">{preview.title}</span>
          {preview.mediaType ? <span className="preview-sub">{preview.mediaType}</span> : null}
          {preview.hash ? <span className="preview-sub">{shortHash(preview.hash)}</span> : null}
          <div className="preview-actions">
            {state.status === "ok" ? <CopyButton text={state.text} /> : null}
            <button className="preview-close" type="button" title="Close (Esc)" onClick={onClose}>
              <X size={15} />
            </button>
          </div>
        </header>
        {preview.note ? <div className="ref-note preview-note">{preview.note}</div> : null}
        {nestedRefs.length ? (
          <div className="preview-refs">
            <span className="preview-refs-label">references · {nestedRefs.length}</span>
            <div className="ref-chip-row">
              {nestedRefs.map((ref) => (
                <RefChip key={`${ref.label}-${ref.hash}`} refInfo={ref} />
              ))}
            </div>
          </div>
        ) : null}
        <div className="preview-body">
          {state.status === "loading" ? (
            <div className="pane-empty">loading…</div>
          ) : imageUrl ? (
            <img className="preview-img" src={imageUrl} alt={preview.title} />
          ) : (
            <pre className="trace-payload preview-pre">{renderPreviewText(state.text || "")}</pre>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function CopyPre({ className, text }) {
  return (
    <div className="copy-wrap">
      <pre className={className}>{text}</pre>
      <button
        className="copy-btn"
        type="button"
        onClick={() => navigator.clipboard?.writeText(text)}
      >
        <Clipboard size={14} />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inspector (right pane)
// ---------------------------------------------------------------------------

// Build a `seq → turn number` resolver from the recorded LLM turns so a
// context action (which is anchored at a plan/event seq) can name the turn it
// belongs to. Turn N = the turn whose request seq first reaches `seq`.
function turnIndexer(context) {
  const selections = Array.isArray(context?.selections) ? context.selections : [];
  const seqs = selections
    .map((s) => (typeof s.seq === "number" ? s.seq : null))
    .filter((v) => v != null)
    .sort((a, b) => a - b);
  return (seq) => {
    if (typeof seq !== "number" || !seqs.length) return null;
    let idx = seqs.findIndex((s) => s >= seq);
    if (idx === -1) idx = seqs.length - 1;
    return idx + 1;
  };
}

// At-a-glance counts of every context-processing kind across the whole task.
function contextStats(context, events) {
  const plans = Array.isArray(context?.plans) ? context.plans : [];
  let compacted = 0;
  let requested = 0;
  for (const env of events || []) {
    if (env?.type === "Compacted") compacted += 1;
    else if (env?.type === "CompactionRequested") requested += 1;
  }
  let microTurns = 0;
  let maxCleared = 0;
  let skillTurns = 0;
  let resourceTurns = 0;
  for (const plan of plans) {
    const d = Array.isArray(plan.dropped_messages) ? plan.dropped_messages.length : 0;
    if (d > 0) {
      microTurns += 1;
      maxCleared = Math.max(maxCleared, d);
    }
    if (Array.isArray(plan.selected_skills) && plan.selected_skills.length) skillTurns += 1;
    if (Array.isArray(plan.retrieved_resources) && plan.retrieved_resources.length) resourceTurns += 1;
  }
  return { turns: plans.length, compacted, requested, microTurns, maxCleared, skillTurns, resourceTurns };
}

// Chronological list of every context-processing action. Discrete compaction
// events (CompactionRequested / Compacted) are listed individually. Micro-
// compaction (tool-output pruning) runs every turn once it kicks in, so it is
// collapsed to escalation milestones — the first turn it activates and each
// turn the cleared-count grows — instead of one near-identical row per turn.
function contextActions(context, events, turnOf) {
  const plans = Array.isArray(context?.plans) ? context.plans : [];
  const out = [];
  for (const env of events || []) {
    if (env?.type === "CompactionRequested") {
      const p = env.payload || {};
      out.push({
        seq: env.seq,
        kind: "compact-req",
        label: "compaction triggered",
        detail:
          (p.reason === "overflow"
            ? "overflow fallback"
            : p.reason === "proactive"
              ? "proactive"
              : p.reason || "") +
          (p.estimated_tokens ? ` · ~${formatTokens(p.estimated_tokens)} tok` : ""),
      });
    } else if (env?.type === "Compacted") {
      const p = env.payload || {};
      out.push({
        seq: env.seq,
        kind: "compacted",
        label: "summary compaction",
        detail: `folded ${p.boundary_count != null ? p.boundary_count : "?"} msgs → summary`,
      });
    }
  }
  let prevDropped = 0;
  const sortedPlans = [...plans]
    .filter((p) => typeof p?.seq === "number")
    .sort((a, b) => a.seq - b.seq);
  for (const plan of sortedPlans) {
    const dropped = Array.isArray(plan.dropped_messages) ? plan.dropped_messages.length : 0;
    if (dropped > prevDropped) {
      out.push({
        seq: plan.seq,
        kind: "micro",
        label: "micro-compaction",
        detail:
          `cleared ${dropped} tool outputs` +
          (prevDropped > 0
            ? ` (+${dropped - prevDropped} vs prev)`
            : " (first trigger, deref-able)"),
      });
    }
    prevDropped = Math.max(prevDropped, dropped);
  }
  out.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  return out.map((a) => ({ ...a, turn: turnOf(a.seq) }));
}

// Right-pane panel answering "what context processing happened, and when?".
// A stats header for the at-a-glance picture, then a clickable timeline of the
// individual actions (jump to the anchoring turn/event on click).
function ContextProcessingSection({ context, events, selectedSeq, onSelect }) {
  const turnOf = turnIndexer(context);
  const stats = contextStats(context, events);
  const actions = contextActions(context, events, turnOf);
  const statLine = [
    `${stats.turns} turns`,
    `summary compaction ${stats.compacted}`,
    `micro-compaction ${stats.microTurns} turns${stats.maxCleared ? ` (max ${stats.maxCleared} cleared)` : ""}`,
    stats.skillTurns ? `skills ${stats.skillTurns} turns` : null,
    stats.resourceTurns ? `resources ${stats.resourceTurns} turns` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="context-view">
      <div className="context-meta">{statLine}</div>
      {actions.length ? (
        actions.map((a, index) => (
          <button
            className={`context-turn ${a.seq === selectedSeq ? "active" : ""}`}
            key={`${a.kind}-${a.seq}-${index}`}
            type="button"
            onClick={() => a.seq != null && onSelect(a.seq)}
          >
            <span>
              <span className="chip">{a.label}</span>
              {a.turn ? ` turn ${a.turn}` : ""}
            </span>
            <small>{a.detail}</small>
          </button>
        ))
      ) : (
        <div className="pane-empty">No compaction / micro-compaction in this session</div>
      )}
    </div>
  );
}

function Inspector({ context, detail, events, selectedSeq, taskId, onSelect }) {
  const artifacts = collectArtifacts(events);
  return (
    <>
      <section className="inspector-section">
        <h2>Context processing</h2>
        <ContextProcessingSection
          context={context}
          events={events}
          selectedSeq={selectedSeq}
          onSelect={onSelect}
        />
      </section>
      <section className="inspector-section">
        <h2>Detail</h2>
        <DetailTable detail={detail} events={events} taskId={taskId} />
      </section>
      <section className="inspector-section">
        <h2>Artifacts</h2>
        {artifacts.length ? (
          <div className="artifact-list">
            {artifacts.map((artifact) => (
              <ArtifactRow key={artifact.hash} artifact={artifact} />
            ))}
          </div>
        ) : (
          <div className="pane-empty">No artifacts</div>
        )}
      </section>
    </>
  );
}

function DetailTable({ detail, events, taskId }) {
  const rows = [
    ["task", taskId ? shortId(taskId) : "-"],
    ["status", detail?.status_text || detail?.status || "unknown"],
    ["wake", detail?.wake_kind || (detail?.wake_on ? JSON.stringify(detail.wake_on) : "")],
    ["model", detail?.model_binding || ""],
    ["closed", detail?.closed ? "yes" : "no"],
    ["events", detail?.event_count || events.length],
  ];
  if (detail?.agent) rows.push(["agent", detail.agent]);
  if (detail?.goal) rows.push(["goal", detail.goal]);
  if (detail?.phase) rows.push(["phase", detail.phase]);
  if (detail?.next_action) rows.push(["next", detail.next_action]);
  if (Array.isArray(detail?.todos) && detail.todos.length) rows.push(["todos", detail.todos.map(planItemLine).join(" | ")]);
  if (Array.isArray(detail?.decisions) && detail.decisions.length) rows.push(["decisions", detail.decisions.map(planItemLine).join(" | ")]);
  if (detail?.context_stats) rows.push(["context", contextStatsLine(detail.context_stats)]);
  return (
    <dl className="detail-table">
      {rows.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value == null || value === "" ? "-" : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function renderContextPlanBody(plan) {
  const resources = Array.isArray(plan?.retrieved_resources) ? plan.retrieved_resources : [];
  return (
    <>
      <dl className="detail-table">
        <div><dt>composer</dt><dd>{plan?.composer_version || "-"}</dd></div>
        <div><dt>skills</dt><dd>{Array.isArray(plan?.selected_skills) && plan.selected_skills.length ? plan.selected_skills.join(", ") : "-"}</dd></div>
        <div><dt>segments</dt><dd>{Object.keys(plan?.segment_hashes || {}).join(", ") || "-"}</dd></div>
        <div><dt>kept msgs</dt><dd>{Array.isArray(plan?.selected_messages) ? plan.selected_messages.length : 0}</dd></div>
        <div><dt>micro-compaction (tool outputs)</dt><dd>{Array.isArray(plan?.dropped_messages) ? plan.dropped_messages.length : 0}</dd></div>
      </dl>
      {resources.length ? <div className="plan-section">retrieved resources</div> : null}
      {resources.map((resource, index) => <PlanResource key={index} resource={resource} />)}
      <div className="ref-note">
        segment / resource / message hashes are identifiers. Open the matching LLM turn to read the bytes.
      </div>
    </>
  );
}

function PlanResource({ resource }) {
  const where = [resource?.skill, resource?.relpath].filter(Boolean).join("/") || "resource";
  const ref = resource?.content_ref;
  return (
    <div className="context-meta">
      {where} · {resource?.reason || "?"}{ref?.hash ? ` · ${shortHash(ref.hash)}` : ""}{resource?.bytes != null ? ` · ${resource.bytes}b` : ""}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Flatten any message (or bare string) to display/copy text + token estimate.
function messageText(msg) {
  if (typeof msg === "string") return msg;
  const blocks = Array.isArray(msg?.content) ? msg.content : [];
  return blocks
    .map((block) => {
      const tag = block?.__canonical_tag__;
      if (tag === "text_block" || tag === "thinking_block") return block.text || "";
      if (tag === "tool_use_block") {
        return `→ ${block.tool_name || "tool"} ${safeJson(block.arguments || {})}`;
      }
      if (tag === "tool_result_block") {
        return typeof block.output === "string" ? block.output : safeJson(block.output);
      }
      return safeJson(block);
    })
    .join("\n");
}

function tokenBadge(text) {
  return `~${formatTokens(approxTokens(text))} tok`;
}

// First-line preview shown on a collapsed message card.
function previewLine(text) {
  const s = (typeof text === "string" ? text : "").replace(/\s+/g, " ").trim();
  if (!s) return "(empty)";
  return s.length > 96 ? `${s.slice(0, 96)}…` : s;
}

// Match a turn to its recorded provenance: the latest context plan at or before
// the request, and the selection summary sharing the request's call_id.
function turnProvenance(context, requestSeq, callId) {
  const plans = Array.isArray(context?.plans) ? context.plans : [];
  const selections = Array.isArray(context?.selections) ? context.selections : [];
  let plan = null;
  for (const candidate of plans) {
    if (typeof candidate.seq !== "number") continue;
    if (requestSeq != null && candidate.seq > requestSeq) continue;
    if (!plan || candidate.seq > plan.seq) plan = candidate;
  }
  if (!plan && plans.length) plan = plans[plans.length - 1];
  let selection = null;
  if (callId) selection = selections.find((item) => item.call_id === callId) || null;
  if (!selection && requestSeq != null) {
    selection = selections.find((item) => item.seq === requestSeq) || null;
  }
  return { plan, selection };
}

// The content-channel residents (skill / memory / environment / instructions)
// active for a turn, read from the ContextContentRecorded events at or before
// the turn's request seq. Surfaces the env / instructions injections the
// composer brought in — visible in the trace even though they are not message
// content. Dedup by kind:name keeping the latest recording (re-activation just
// rerecords the same name with a fresh hash). `requestSeq` scopes it to the
// turn; with no seq it falls back to every recorded resident.
function activeResidents(events, requestSeq) {
  const byKey = new Map();
  for (const env of events || []) {
    if (env?.type !== "ContextContentRecorded") continue;
    if (typeof requestSeq === "number" && typeof env.seq === "number" && env.seq > requestSeq) continue;
    const p = env.payload || {};
    if (!p.kind || !p.name) continue;
    byKey.set(`${p.kind}:${p.name}`, {
      kind: p.kind,
      name: p.name,
      policy: p.policy || null,
      seq: env.seq,
    });
  }
  return [...byKey.values()].sort(
    (a, b) => String(a.kind).localeCompare(String(b.kind)) || String(a.name).localeCompare(String(b.name)),
  );
}

function collectPayloadRefs(payload) {
  const refs = [];
  const walk = (obj, path) => {
    if (!obj || typeof obj !== "object") return;
    if (obj.__canonical_tag__ === "content_ref" && typeof obj.hash === "string") {
      refs.push({
        label: path || "content_ref",
        hash: obj.hash,
        mediaType: obj.media_type || obj.mediaType || null,
        size: typeof obj.size === "number" ? obj.size : null,
      });
      return;
    }
    if (Array.isArray(obj)) obj.forEach((value, index) => walk(value, `${path}[${index}]`));
    else for (const [key, value] of Object.entries(obj)) walk(value, path ? `${path}.${key}` : key);
  };
  walk(payload, "");
  return refs;
}

function refHash(ref) {
  return typeof ref?.hash === "string" ? ref.hash : null;
}

function refIsManifest(label) {
  return typeof label === "string" && (label === "plan_ref" || label.endsWith(".plan_ref"));
}

function sessionSummary(events, detail) {
  let inputTokens = 0;
  let outputTokens = 0;
  let cacheReadTokens = 0;
  let cacheWriteTokens = 0;
  let costUsd = 0;
  let llmTurns = 0;
  let model = null;
  let first = null;
  let last = null;
  for (const env of events) {
    const payload = env?.payload || {};
    if (typeof env?.occurred_at === "number") {
      if (first == null || env.occurred_at < first) first = env.occurred_at;
      if (last == null || env.occurred_at > last) last = env.occurred_at;
    }
    if (env?.type === "LLMRequestStarted") {
      llmTurns += 1;
      if (!model && payload.model) model = payload.model;
    } else if (env?.type === "LLMRequestFinished") {
      // Real provider-reported usage. Each call bills its full input (history
      // re-sent every turn), so summing per-call input is the total tokens
      // consumed — not the context size, which would be just the last turn.
      const u = usageTokens(payload.usage);
      if (u) {
        inputTokens += u.input;
        outputTokens += u.output;
        cacheReadTokens += u.cacheRead;
        cacheWriteTokens += u.cacheWrite;
      }
      if (typeof payload.cost_usd === "number") costUsd += payload.cost_usd;
    } else if (env?.type === "ModelBound" && !model && payload.model) {
      model = payload.model;
    }
  }
  return {
    model: model || detail?.model_binding || "-",
    eventCount: events.length,
    llmTurns,
    inputTokens,
    outputTokens,
    cacheReadTokens,
    cacheWriteTokens,
    totalTokens: inputTokens + outputTokens,
    costUsd,
    duration: first != null && last != null ? last - first : null,
  };
}

function rowMatchesFilter(row, filterText, activeCategories) {
  if (activeCategories.size && !activeCategories.has(row.category)) return false;
  const query = filterText.trim().toLowerCase();
  if (!query) return true;
  return `${row.type || ""} ${row.summary || ""}`.toLowerCase().includes(query);
}

// Build a seq -> { clock, delta } map in a single pass over the session rows.
// `clock` is the time since the session's first timestamp; `delta` is the gap
// from the previous row that carries a timestamp. Precomputing once avoids the
// O(N) per-row scans (session-min + backward findIndex) that made the timeline
// O(N^2) in event count on every re-render.
function sessionClocks(rows) {
  let start = null;
  for (const row of rows) {
    if (typeof row.occurredAt === "number" && (start == null || row.occurredAt < start)) {
      start = row.occurredAt;
    }
  }
  const clocks = new Map();
  let prev = null;
  for (const row of rows) {
    const at = row.occurredAt;
    const clock = at != null && start != null ? `+${formatDuration(at - start)}` : "";
    const delta = at != null && prev != null ? `Δ${formatDuration(at - prev)}` : "";
    clocks.set(row.seq, { clock, delta });
    if (at != null) prev = at;
  }
  return clocks;
}

function planItemLine(item) {
  if (item == null || typeof item !== "object") return String(item);
  const parts = [];
  if (item.status != null) parts.push(`[${item.status}]`);
  for (const key of ["content", "text", "title", "task", "id"]) {
    if (item[key] != null) {
      parts.push(String(item[key]).slice(0, 60));
      break;
    }
  }
  return parts.length ? parts.join(" ") : "(item)";
}

function contextStatsLine(stats) {
  return [
    `selected ${stats.selected_message_count || 0}`,
    `dropped ${stats.dropped_message_count || 0}`,
    `request ${stats.request_bytes == null ? "?" : `${stats.request_bytes}b`}`,
    `tokens ${stats.input_tokens_available ? stats.input_tokens : "?"}`,
  ].join(" · ");
}

export { TraceApp };
