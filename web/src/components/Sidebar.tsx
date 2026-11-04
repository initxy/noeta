import type { TaskSummary } from "../api/types";
import { sessionStatus } from "../state/transcript";

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

/** The sessions, newest first, grouped by project directory. */
export function Sidebar({
  sessions,
  current,
  onOpen,
  onNew,
}: {
  sessions: TaskSummary[];
  current: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const groups = new Map<string, TaskSummary[]>();
  for (const s of sessions) groups.set(s.cwd, [...(groups.get(s.cwd) ?? []), s]);
  return (
    <nav className="sidebar" aria-label="Sessions">
      <button className="new-session" onClick={onNew}>
        + New session
      </button>
      {[...groups.entries()].map(([cwd, list]) => (
        <section key={cwd} className="project">
          <div className="project-name" title={cwd}>
            {basename(cwd)}
          </div>
          <ul>
            {list.map((s) => {
              const status = sessionStatus(s.status, s.waiting_on);
              return (
                <li key={s.task_id}>
                  <button className={s.task_id === current ? "session active" : "session"} onClick={() => onOpen(s.task_id)} title={s.prompt}>
                    <span className={`status status-${status}`} aria-label={STATUS_NAMES[status]} title={STATUS_NAMES[status]} />
                    <span className="session-title">{s.title || s.prompt || "Untitled session"}</span>
                    <span className="muted">{ago(s.updated_at)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {!sessions.length && <div className="muted empty">No sessions yet</div>}
    </nav>
  );
}
