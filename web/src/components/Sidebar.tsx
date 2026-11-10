import { useState } from "react";
import type { Project, TaskSummary } from "../api/types";
import { basename, loadFolded, projectFor, projectList, saveFolded } from "../state/projects";
import { sessionStatus } from "../state/transcript";

export type SessionStatus = ReturnType<typeof sessionStatus>;

const STATUS_NAMES = { running: "Running", "needs-you": "Needs you", idle: "Waiting", ended: "Ended" };

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

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
      {open ? (
        <path d="M1.5 4.5v8a1 1 0 0 0 1 1h9.6a1 1 0 0 0 .95-.7l1.6-5a.6.6 0 0 0-.57-.8H4.3a1 1 0 0 0-.95.7L1.6 13M1.5 4.5a1 1 0 0 1 1-1h3.3l1.4 1.5h4.8a1 1 0 0 1 1 1V7" />
      ) : (
        <path d="M1.5 4.5a1 1 0 0 1 1-1h3.3l1.4 1.5h6.3a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z" />
      )}
    </svg>
  );
}

/** Whether a session's title, first prompt or folder holds every word of `query`. */
export function matches(s: TaskSummary, query: string, title = s.title): boolean {
  return holds(`${title}\n${s.prompt}\n${s.cwd}`, query);
}

function holds(text: string, query: string): boolean {
  const hay = text.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .every((w) => hay.includes(w));
}

/** A project's "…" menu: what can be done to the project itself. */
function ProjectMenu({ name, onRemove }: { name: string; onRemove: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className="project-menu"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
    >
      <button className="icon-button row-action" aria-label={`${name} options`} title="Options" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        ⋯
      </button>
      {open && (
        <div className="menu" role="menu">
          <button
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onRemove();
            }}
          >
            Remove project
          </button>
        </div>
      )}
    </div>
  );
}

/** The left column: a new session, then each project (folder) with its
 * sessions newest first, narrowed by the search box. The open one shows
 * its `live` title and status (what its event stream says now).
 * `archived`: the list is of archived sessions, by folder; each row's
 * button archives or restores it (`onArchive`). */
export function Sidebar({
  sessions,
  projects = null,
  project = "",
  current,
  live,
  archived = false,
  onOpen,
  onNew,
  onSelectProject,
  onNewIn,
  onAddProject,
  onRemoveProject,
  onArchive,
  onShowArchived,
  onDisconnect,
}: {
  sessions: TaskSummary[];
  /** The server's projects (null: it keeps none); the sessions' folders and the selected one are added. The archived list shows its sessions' folders only. */
  projects?: Project[] | null;
  /** The selected project's path. */
  project?: string;
  current: string | null;
  live?: { title: string; status: SessionStatus };
  archived?: boolean;
  onOpen: (id: string) => void;
  onNew: () => void;
  onSelectProject?: (path: string) => void;
  onNewIn?: (path: string) => void;
  onAddProject?: () => void;
  onRemoveProject?: (path: string) => void;
  onArchive?: (id: string, archived: boolean) => void;
  onShowArchived?: (show: boolean) => void;
  onDisconnect?: () => void;
}) {
  const [query, setQuery] = useState("");
  const [folded, setFolded] = useState(loadFolded);
  const q = query.trim();
  const listed: Project[] = archived
    ? [...new Set(sessions.map((s) => s.cwd))].map((path) => ({ path, name: basename(path), saved: false, sessions: 0 }))
    : projectList(projects, sessions, project);
  // Sessions by the project they run in (its path, which may be a session's own spelled through a symlink).
  const byDir = new Map<string, TaskSummary[]>();
  for (const s of [...sessions].sort((a, b) => b.updated_at - a.updated_at)) {
    const title = (s.task_id === current ? live?.title : "") || s.title;
    if (q && !matches(s, q, title)) continue;
    const dir = projectFor(s.cwd, listed)?.path ?? s.cwd;
    byDir.set(dir, [...(byDir.get(dir) ?? []), s]);
  }
  const shown = listed.filter((p) => byDir.has(p.path) || (!archived && (!q || holds(`${p.name}\n${p.path}`, q))));
  const fold = (path: string) => {
    const next = new Set(folded);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    saveFolded(next);
    setFolded(next);
  };
  return (
    <nav className="sidebar" aria-label="Sessions">
      <button className="new-session" onClick={onNew} title={project ? `New session in ${project}` : "New session"}>
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
      <div className="list-heading">
        <span>{archived ? "Archived" : "Projects"}</span>
        {archived ? (
          onShowArchived && (
            <button className="link" onClick={() => onShowArchived(false)}>
              Back to sessions
            </button>
          )
        ) : (
          onAddProject && (
            <button className="icon-button add-project" aria-label="Add project" title="Add project" onClick={onAddProject}>
              +
            </button>
          )
        )}
      </div>
      <div className="project-list">
        {shown.map((p) => {
          const list = byDir.get(p.path) ?? [];
          const open = !!q || archived || !folded.has(p.path);
          return (
            <section key={p.path} className="project" data-project={p.path}>
              <div className={!archived && p.path === project && !current ? "project-row selected" : "project-row"}>
                <button
                  className="icon-button fold"
                  aria-label={open ? `Fold ${p.name}` : `Unfold ${p.name}`}
                  aria-expanded={open}
                  disabled={archived || !!q}
                  onClick={() => fold(p.path)}
                >
                  <FolderIcon open={open} />
                </button>
                <button
                  className="project-name"
                  title={p.path}
                  disabled={archived}
                  onClick={() => {
                    if (folded.has(p.path)) fold(p.path);
                    onSelectProject?.(p.path);
                  }}
                >
                  {p.name}
                </button>
                {!archived && onNewIn && (
                  <button className="icon-button row-action" aria-label={`New session in ${p.name}`} title="New session here" onClick={() => onNewIn(p.path)}>
                    +
                  </button>
                )}
                {!archived && p.saved && onRemoveProject && <ProjectMenu name={p.name} onRemove={() => onRemoveProject(p.path)} />}
              </div>
              {open && (
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
                  {!list.length && !q && <li className="muted empty no-sessions">No sessions yet</li>}
                </ul>
              )}
            </section>
          );
        })}
        {!shown.length && (
          <div className="muted empty">
            {q && (sessions.length || listed.length) ? "No sessions match" : archived ? "No archived sessions" : "No projects yet"}
          </div>
        )}
      </div>
      <div className="sidebar-footer">
        {!archived && onShowArchived && (
          <button className="link" onClick={() => onShowArchived(true)}>
            Archived
          </button>
        )}
        {onDisconnect && (
          <button className="link disconnect" title="Forget this server's token on this browser" onClick={onDisconnect}>
            Disconnect
          </button>
        )}
      </div>
    </nav>
  );
}
