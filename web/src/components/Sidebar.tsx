import { useState } from "react";
import type { TaskSummary } from "../api/types";
import { sessionStatus } from "../state/transcript";

export type SessionStatus = ReturnType<typeof sessionStatus>;

const STATUS_NAMES = { running: "Running", "needs-you": "Needs you", idle: "Waiting", ended: "Ended" };

function basename(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() || path || "server";
}

function ago(at: number): string {
  const s = Math.max(0, Date.now() / 1000 - at);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function ArchiveIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
      <rect x="1.5" y="2.5" width="13" height="3.5" rx="1" />
      <path d="M2.5 6v6.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V6M6.5 9h3" />
    </svg>
  );
}

/** Whether a session's title, first prompt or folder holds every word of `query`. */
export function matches(s: TaskSummary, query: string, title = s.title): boolean {
  const hay = `${title}\n${s.prompt}\n${s.cwd}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .every((w) => hay.includes(w));
}

/** The sessions, newest first, grouped by project directory, narrowed by
 * the search box. The open one shows its `live` title and status (what its
 * event stream says now). `archived`: the list is of archived sessions;
 * each row's button archives or restores it (`onArchive`). */
export function Sidebar({
  sessions,
  current,
  live,
  archived = false,
  onOpen,
  onNew,
  onArchive,
  onShowArchived,
  onDisconnect,
}: {
  sessions: TaskSummary[];
  current: string | null;
  live?: { title: string; status: SessionStatus };
  archived?: boolean;
  onOpen: (id: string) => void;
  onNew: () => void;
  onArchive?: (id: string, archived: boolean) => void;
  onShowArchived?: (show: boolean) => void;
  onDisconnect?: () => void;
}) {
  const [query, setQuery] = useState("");
  const groups = new Map<string, TaskSummary[]>();
  for (const s of sessions) {
    const title = (s.task_id === current ? live?.title : "") || s.title;
    if (query.trim() && !matches(s, query.trim(), title)) continue;
    groups.set(s.cwd, [...(groups.get(s.cwd) ?? []), s]);
  }
  return (
    <nav className="sidebar" aria-label="Sessions">
      <button className="new-session" onClick={onNew}>
        + New session
      </button>
      <input
        className="session-search"
        type="search"
        aria-label="Search sessions"
        placeholder="Search sessions"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && setQuery("")}
      />
      {onShowArchived && (
        <div className="list-heading">
          <span>{archived ? "Archived" : "Recent"}</span>
          <button className="link" onClick={() => onShowArchived(!archived)}>
            {archived ? "Back to sessions" : "Archived"}
          </button>
        </div>
      )}
      {[...groups.entries()].map(([cwd, list]) => (
        <section key={cwd} className="project">
          <div className="project-name" title={cwd}>
            {basename(cwd)}
          </div>
          <ul>
            {list.map((s) => {
              const mine = s.task_id === current ? live : undefined;
              const status = mine?.status ?? sessionStatus(s.status, s.waiting_on);
              const title = mine?.title || s.title || s.prompt || "Untitled session";
              return (
                <li key={s.task_id} className="session-row">
                  <button className={s.task_id === current ? "session active" : "session"} onClick={() => onOpen(s.task_id)} title={s.prompt}>
                    <span className={`status status-${status}`} aria-label={STATUS_NAMES[status]} title={STATUS_NAMES[status]} />
                    <span className="session-title">{title}</span>
                    <span className="muted ago">{ago(s.updated_at)}</span>
                  </button>
                  {onArchive && (
                    <button
                      className="icon-button row-action"
                      aria-label={archived ? "Unarchive" : "Archive"}
                      title={archived ? "Unarchive" : "Archive"}
                      onClick={() => onArchive(s.task_id, !archived)}
                    >
                      {archived ? "↺" : <ArchiveIcon />}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {!groups.size && (
        <div className="muted empty">{sessions.length ? "No sessions match" : archived ? "No archived sessions" : "No sessions yet"}</div>
      )}
      {onDisconnect && (
        <button className="link disconnect" title="Forget this server's token on this browser" onClick={onDisconnect}>
          Disconnect
        </button>
      )}
    </nav>
  );
}
