import {
  Check,
  ChevronDown,
  FolderPlus,
  Pin,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../components/EmptyState.jsx";
import { shortId } from "../lib/format.js";
import { ICON_SM } from "../shared/icons.js";
import { humanSessionStatus, sessionDotClass } from "./chat-shared.js";
import {
  filterSessionsBySearch,
  loadPinnedSessions,
  partitionPinned,
  savePinnedSessions,
} from "./sidebar-prefs.js";

// codex addendum (workspace-and-session-path.md): a collapsed "+ New project"
// button that expands into a tiny form — an absolute path + optional name —
// POSTed through chat.createWorkspace. The path is validated server-side (zero
// whitelist); a 400 surfaces via the shared notice banner, and the
// form only resets / closes on a successful create.
function NewProjectControl({ onCreate, disabled }) {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const close = () => {
    setOpen(false);
    setPath("");
    setName("");
  };
  const submit = async () => {
    const trimmed = path.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    const created = await onCreate?.({ path: trimmed, name: name.trim() || undefined });
    setBusy(false);
    if (created) close();
  };
  const onKeyDown = (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    } else if (event.key === "Escape") {
      close();
    }
  };

  if (!open) {
    return (
      <button
        className="new-project-btn"
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <FolderPlus size={ICON_SM} />
        New project
      </button>
    );
  }
  return (
    <div className="new-project-form" aria-label="New project">
      <input
        type="text"
        className="new-project-input"
        placeholder="/absolute/path/to/project"
        value={path}
        autoFocus
        onChange={(event) => setPath(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <input
        type="text"
        className="new-project-input"
        placeholder="Name (optional)"
        value={name}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="new-project-form__actions">
        <button type="button" onClick={close}>
          Cancel
        </button>
        <button
          type="button"
          className="new-project-form__add"
          disabled={!path.trim() || busy}
          onClick={submit}
        >
          Add
        </button>
      </div>
    </div>
  );
}

// group the session list by ``workspace_dir`` (the welded ABSOLUTE
// path). Sessions sharing a registered workspace's path collapse into one named
// group (title = the registry ``name`` mapped from that path); bare sessions —
// whose ``workspace_dir`` is a private ``session-<uuid>`` dir not in the
// registry (or null on old recordings) — each land in the catch-all "Ungrouped"
// bucket and show a short directory name. Group order: named workspaces first
// (registry order, stable), then ungrouped.
function groupSessionsByWorkspace(rows, workspaces) {
  const nameByPath = new Map();
  const orderByPath = new Map();
  (Array.isArray(workspaces) ? workspaces : []).forEach((ws, index) => {
    if (ws && typeof ws.path === "string") {
      nameByPath.set(ws.path, typeof ws.name === "string" ? ws.name : ws.path);
      orderByPath.set(ws.path, index);
    }
  });
  const named = new Map(); // path -> {key, name, order, rows}
  const ungrouped = [];
  for (const row of rows) {
    const dir = row.workspace_dir;
    if (dir && nameByPath.has(dir)) {
      let group = named.get(dir);
      if (!group) {
        group = {
          key: `ws:${dir}`,
          name: nameByPath.get(dir),
          order: orderByPath.get(dir),
          rows: [],
        };
        named.set(dir, group);
      }
      group.rows.push(row);
    } else {
      ungrouped.push(row);
    }
  }
  const groups = [...named.values()].sort((a, b) => a.order - b.order);
  if (ungrouped.length) {
    groups.push({ key: "ungrouped", name: null, order: Infinity, rows: ungrouped });
  }
  return groups;
}

// Collapsed state persists to localStorage, keyed by group.key (the workspace's
// absolute path), so each workspace's expand/collapse survives a refresh. Reads
// and writes are wrapped in try/catch: when localStorage is disabled it degrades
// to "no memory" and never throws.
const COLLAPSED_GROUPS_KEY = "noeta.sidebar.collapsedGroups";

function loadCollapsedGroups() {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_GROUPS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (e) {
    return new Set();
  }
}

function saveCollapsedGroups(set) {
  try {
    window.localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify([...set]));
  } catch (e) {
    /* ignored when localStorage is unavailable */
  }
}

function SessionList({ rows, activeTaskId, workspaces, onSelect, onCancel, onDelete }) {
  // Hooks must be declared unconditionally first (not after an early return),
  // then short-circuit on an empty list.
  const [collapsed, setCollapsed] = useState(loadCollapsedGroups);
  // Two-step delete confirm: the first trash click marks the row "pending"; a ✓
  // click actually deletes; ✗ or switching rows cancels. Fits the app's look
  // better than a native confirm dialog.
  const [pendingDelete, setPendingDelete] = useState(null);
  // codex addendum: pinned task_ids (localStorage, same try/catch contract as
  // collapsed groups) + a client-side title search. Pinned sessions float into a
  // "Pinned" group above the workspace groups; search filters by title.
  const [pinned, setPinned] = useState(loadPinnedSessions);
  const [search, setSearch] = useState("");

  const toggleGroup = useCallback((key) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      saveCollapsedGroups(next);
      return next;
    });
  }, []);

  const togglePin = useCallback((taskId) => {
    setPinned((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      savePinnedSessions(next);
      return next;
    });
  }, []);

  // U7 — Esc cancels an in-flight delete confirmation (keyboard users had no way
  // to back out of the Yes/No state but the mouse). Only one row can be
  // confirming at a time (single pendingDelete), so clicking another row's trash
  // already moves the confirm; switching the active session clears it too, so a
  // stale Yes/No never lingers on a row the user navigated away from.
  useEffect(() => {
    const onEsc = (event) => {
      if (event.key === "Escape") setPendingDelete(null);
    };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
  }, []);
  useEffect(() => {
    setPendingDelete(null);
  }, [activeTaskId]);

  if (!rows.length)
    return <EmptyState kind="session" title="No sessions yet" />;

  const filtered = filterSessionsBySearch(rows, search);
  const { pinnedRows, rest } = partitionPinned(filtered, pinned);
  const groups = groupSessionsByWorkspace(rest, workspaces);
  if (pinnedRows.length) {
    groups.unshift({ key: "__pinned__", name: "Pinned", order: -1, rows: pinnedRows });
  }

  const renderRow = (row) => {
    // B8 — keep the RAW status for logic (dot colour + running check); only the
    // user-facing text is humanised (unknown → Active, closed → Closed).
    const rawState = row.closed ? "closed" : row.status || "unknown";
    const dispatcherMismatch =
      row.dispatcher_status && row.dispatcher_status !== row.status;
    const stateText = humanSessionStatus(rawState);
    const stateTitle = dispatcherMismatch
      ? `${stateText} · Dispatcher ${humanSessionStatus(row.dispatcher_status)}`
      : stateText;
    const confirming = pendingDelete === row.task_id;
    const running = String(rawState).toLowerCase().includes("run");
    const isPinned = pinned.has(row.task_id);
    return (
      <div
        className={`session-row ${
          row.task_id === activeTaskId ? "active" : ""
        }${confirming ? " confirming" : ""}`}
        key={row.task_id}
      >
        <button
          className="session-row__main"
          type="button"
          onClick={() => onSelect(row.task_id)}
        >
          <span
            className={`session-dot ${sessionDotClass(rawState, row.dispatcher_status)}`}
            title={stateTitle}
            aria-label={stateTitle}
          />
          <span className="session-main">
            <span className="session-title">
              {row.title || shortId(row.task_id)}
            </span>
          </span>
        </button>
        <button
          className={`icon-button session-row__pin${isPinned ? " is-pinned" : ""}`}
          type="button"
          title={isPinned ? "Unpin" : "Pin to top"}
          aria-label={isPinned ? "Unpin session" : "Pin session"}
          aria-pressed={isPinned}
          onClick={() => togglePin(row.task_id)}
        >
          <Pin size={ICON_SM} />
        </button>
        {running ? (
          <button
            className="icon-button session-row__stop"
            type="button"
            title="Stop the running session"
            onClick={() => onCancel?.(row.task_id)}
          >
            <Square size={ICON_SM} />
          </button>
        ) : null}
        {confirming ? (
          <span className="session-row__confirm">
            <button
              className="icon-button session-row__del-yes"
              type="button"
              title="Confirm delete (all data, unrecoverable)"
              onClick={() => {
                setPendingDelete(null);
                onDelete?.(row.task_id);
              }}
            >
              <Check size={ICON_SM} />
            </button>
            <button
              className="icon-button session-row__del-no"
              type="button"
              title="Cancel"
              onClick={() => setPendingDelete(null)}
            >
              <X size={ICON_SM} />
            </button>
          </span>
        ) : (
          <button
            className="icon-button session-row__del"
            type="button"
            title="Delete the session and all its data"
            onClick={() => setPendingDelete(row.task_id)}
          >
            <Trash2 size={ICON_SM} />
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="session-list-wrap">
      <div className="session-search">
        <Search size={ICON_SM} className="session-search__icon" aria-hidden="true" />
        <input
          type="text"
          className="session-search__input"
          placeholder="Search sessions"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {search ? (
          <button
            className="icon-button session-search__clear"
            type="button"
            title="Clear search"
            aria-label="Clear search"
            onClick={() => setSearch("")}
          >
            <X size={ICON_SM} />
          </button>
        ) : null}
      </div>
      {groups.length ? (
        <div className="session-list">
          {groups.map((group) => {
            const isCollapsed = collapsed.has(group.key);
            return (
              <div className="session-group" key={group.key}>
                <button
                  className="session-group__title"
                  type="button"
                  onClick={() => toggleGroup(group.key)}
                  aria-expanded={!isCollapsed}
                  title={isCollapsed ? "Expand" : "Collapse"}
                >
                  <ChevronDown
                    size={ICON_SM}
                    className={`session-group__chevron${
                      isCollapsed ? " is-collapsed" : ""
                    }`}
                  />
                  <span className="session-group__name">
                    {group.name == null ? "Ungrouped" : group.name}
                  </span>
                  <span className="session-group__count">{group.rows.length}</span>
                </button>
                {isCollapsed ? null : group.rows.map((row) => renderRow(row))}
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState kind="session" title="No matching sessions" />
      )}
    </div>
  );
}

export { NewProjectControl, SessionList };
