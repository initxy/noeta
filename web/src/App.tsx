import { Slot, SLOTS, SlotsContext } from "@initxy/noeta-web-sdk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Api, ApiError, forgetConnection, initialConnection, saveConnection, type Connection } from "./api/client";
import type { Command, Input, ModelInfo, Pending, Project, TaskSummary, TreeUsage } from "./api/types";
import { Composer, newComposerKeep, type Attachment } from "./components/Composer";
import { ModelPicker, NEW_SESSION_MODES, NewSessionMode, RewindDialog, UsageBadge } from "./components/Controls";
import { FolderPicker } from "./components/FolderPicker";
import { FollowScroll } from "./components/FollowScroll";
import { PendingCards } from "./components/PendingCard";
import { PluginsDialog } from "./components/PluginsDialog";
import { Notices, StatusBar, Widgets } from "./components/PluginUi";
import { Sidebar, type SessionStatus } from "./components/Sidebar";
import { LiveTranscript } from "./components/Transcript";
import { attentionChanges, desktop } from "./desktop";
import { ThemeSource } from "./plugins/context";
import { TaskEvents } from "./plugins/events";
import { usePlugins, useUiItems } from "./plugins/usePlugins";
import { continuationConfig, newSessionConfig } from "./state/newTask";
import { basename, loadLastProject, projectFor, saveLastProject, scratchDay, scratchRoot, shortPath } from "./state/projects";
import type { TaskState } from "./state/transcript";
import { usePoll } from "./state/usePoll";
import { ApiContext, useTask, useTaskView } from "./state/useTask";

/** Commands the page runs itself, as Claude Code's app does. */
const PAGE_COMMANDS: Command[] = [
  { name: "clear", description: "Start a new session", argument_hint: "", local: true },
  { name: "rename", description: "Rename this session", argument_hint: "<title>", local: true },
  { name: "model", description: "Switch the model", argument_hint: "<model>", local: true },
  { name: "rewind", description: "Go back to before a prompt", argument_hint: "", local: true },
  { name: "plugins", description: "Switch, install and remove plugins", argument_hint: "", local: true },
];

function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("noeta.theme") ?? "";
    } catch {
      return "";
    }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }, [theme]);
  const dark = theme ? theme === "dark" : window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const toggle = () => {
    const next = dark ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("noeta.theme", next);
    } catch {
      // per-page only
    }
  };
  return [dark ? "dark" : "light", toggle];
}

function Connect({ initial, error, onConnect }: { initial: Connection | null; error: string; onConnect: (c: Connection) => void }) {
  const [server, setServer] = useState(initial?.server ?? "");
  const [token, setToken] = useState(initial?.token ?? "");
  return (
    <div className="connect">
      <form
        className="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          onConnect({ server: server.trim(), token: token.trim() });
        }}
      >
        <h1>noeta</h1>
        <p className="muted">Connect to a `noeta serve` server.</p>
        {error && <div className="notice error">{error}</div>}
        <label>
          Server URL
          <input value={server} placeholder="this page's server" onChange={(e) => setServer(e.target.value)} />
        </label>
        <label>
          Bearer token
          <input type="password" value={token} autoComplete="off" onChange={(e) => setToken(e.target.value)} />
        </label>
        <button className="primary" type="submit">
          Connect
        </button>
      </form>
    </div>
  );
}

const REPLACED_KEY = "noeta.replaced";

/** Sessions this browser rewound or continued into a new one: the sidebar
 * shows the new one only. */
function loadReplaced(): Set<string> {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(REPLACED_KEY) ?? "[]");
    return new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function saveReplaced(ids: Set<string>): void {
  try {
    localStorage.setItem(REPLACED_KEY, JSON.stringify([...ids].slice(-500)));
  } catch {
    // per-page only
  }
}

/** What the page around the transcript reads of the open task: none of it
 * changes while text streams, so streaming re-renders none of it. */
const shellOf = (s: TaskState) => ({
  lastSeq: s.lastSeq,
  pluginsOff: s.pluginsOff,
  cwd: s.cwd,
  model: s.model,
  busy: s.busy,
  ended: s.ended,
  waitingOn: s.waitingOn,
  waitingIds: s.waitingIds,
  running: s.running,
  replied: s.replied,
  contextTokens: s.contextTokens,
});

/** The open session's status from its event stream (fresher than a listing). */
function liveStatus(state: Pick<TaskState, "ended" | "waitingOn" | "busy">): SessionStatus {
  if (state.ended !== null) return "ended";
  if (state.waitingOn === "approval" || state.waitingOn === "question") return "needs-you";
  return state.busy ? "running" : "idle";
}

function sessionFromHash(): string | null {
  const m = /^#session=([\w.-]+)$/.exec(window.location.hash);
  return m ? m[1] : null;
}

export function App() {
  const [conn, setConn] = useState<Connection | null>(initialConnection);
  const [connError, setConnError] = useState("");
  const [theme, toggleTheme] = useTheme();
  const api = useMemo(() => (conn ? new Api(conn) : null), [conn]);
  if (!api) {
    return (
      <Connect
        initial={conn}
        error={connError}
        onConnect={(c) => {
          saveConnection(c);
          setConnError("");
          setConn(c);
        }}
      />
    );
  }
  return (
    <ApiContext.Provider value={api}>
      <Workspace
        api={api}
        theme={theme}
        toggleTheme={toggleTheme}
        onUnauthorized={(msg) => {
          setConnError(msg);
          setConn(null);
        }}
        onDisconnect={
          window.NOETA_CONFIG?.token !== undefined
            ? undefined
            : () => {
                forgetConnection();
                setConnError("");
                setConn(null);
              }
        }
      />
    </ApiContext.Provider>
  );
}

function Workspace({
  api,
  theme,
  toggleTheme,
  onUnauthorized,
  onDisconnect,
}: {
  api: Api;
  theme: string;
  toggleTheme: () => void;
  onUnauthorized: (m: string) => void;
  onDisconnect?: () => void;
}) {
  const [sessions, setSessions] = useState<TaskSummary[]>([]);
  const [taskId, setTaskId] = useState<string | null>(sessionFromHash);
  const events = useMemo(() => (taskId ? new TaskEvents(taskId) : null), [taskId]);
  const store = useTask(api, taskId, events ? (rec, from) => (from ? events.keep(rec) : events.emit(rec)) : undefined);
  const state = useTaskView(store, shellOf);
  const dispatch = store.dispatch;
  const [pending, setPending] = useState<Pending[]>([]);
  const [commands, setCommands] = useState<Command[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [usage, setUsage] = useState<TreeUsage | null>(null);
  const [newMode, setNewMode] = useState("default");
  const [newModel, setNewModel] = useState("");
  const [rewinding, setRewinding] = useState(false);
  const [pluginsOpen, setPluginsOpen] = useState(false);
  const [draft, setDraft] = useState<{ text: string; n: number } | undefined>();
  const [sent, setSent] = useState(0); // a message sent takes the view to the end
  const [composerKeep] = useState(newComposerKeep);
  const [replaced, setReplaced] = useState(loadReplaced);
  const [navOpen, setNavOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState("");
  const desk = desktop();
  const [project, setProject] = useState(() => (desk ? "" : loadLastProject()));
  // The server's projects; null: it has no `/projects` (the list comes from the sessions).
  const [served, setServed] = useState<Project[] | null>(null);
  const [picking, setPicking] = useState(false);
  const listed = useRef<TaskSummary[] | null>(null);
  const [themeSource] = useState(() => new ThemeSource(theme === "dark" ? "dark" : "light"));
  useEffect(() => themeSource.set(theme === "dark" ? "dark" : "light"), [theme, themeSource]);
  // A task's web parts start once its log is read (what it runs without is known then).
  const plugins = usePlugins(api, state.lastSeq ? events : null, state.pluginsOff, themeSource, state.cwd || project);
  const uiItems = useUiItems(api, taskId, state.lastSeq);
  // The title as the server lists it; the title plugin shows it live.
  const title = sessions.find((s) => s.task_id === taskId)?.title ?? "";


  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onUnauthorized("The server refused the token.");
      else setError(String(e instanceof Error ? e.message : e));
    },
    [onUnauthorized],
  );

  const open = useCallback((id: string | null) => {
    setTaskId(id);
    setPending([]);
    setUsage(null);
    setNavOpen(false);
    history.replaceState(null, "", id ? `#session=${id}` : window.location.pathname);
  }, []);

  const refreshSessions = useCallback(
    () =>
      api.summaries(50, showArchived).then((list) => {
        if (desk && listed.current) for (const a of attentionChanges(listed.current, list)) desk.notify(a);
        listed.current = list;
        setSessions(list);
      }, fail),
    [api, fail, desk, showArchived],
  );

  const refreshProjects = useCallback(
    () =>
      api.projects().then(setServed, (e: unknown) => {
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) setServed(null);
        else fail(e);
      }),
    [api, fail],
  );
  useEffect(() => void refreshProjects(), [refreshProjects]);

  // A session in a folder the projects do not cover yet (the first scratch
  // session, one made elsewhere) asks the server again, once per folder.
  const askedFor = useRef(new Set<string>());
  useEffect(() => {
    if (!served) return;
    const scratch = scratchRoot(served);
    const unknown = sessions.some(
      (s) =>
        s.cwd &&
        !askedFor.current.has(s.cwd) &&
        !projectFor(s.cwd, served) &&
        !scratchDay(s.cwd, scratch) &&
        askedFor.current.add(s.cwd),
    );
    if (unknown) void refreshProjects();
  }, [sessions, served, refreshProjects]);

  /** The project new sessions run in, remembered for next time. */
  const pickProject = useCallback(
    (path: string) => {
      setProject(path);
      if (desk) desk.setProject?.(path);
      else saveLastProject(path);
    },
    [desk],
  );

  useEffect(() => {
    if (!desk) return;
    void desk.project().then(setProject);
    return desk.onCommand((c) => {
      if (c.name === "new-session") open(null);
      else if (c.name === "open-session") open(c.taskId);
      else {
        setProject(c.path);
        open(null);
        void refreshProjects();
      }
    });
  }, [desk, open, refreshProjects]);

  // A browser's remembered project the server no longer lists is forgotten
  // (once, and only while it is still the one picked).
  const remembered = useRef<string | null>(project);
  useEffect(() => {
    if (desk || !served || remembered.current === null) return;
    if (project === remembered.current && project && !projectFor(project, served)) pickProject("");
    remembered.current = null;
  }, [desk, served, project, pickProject]);

  // Opening a session makes its folder the project a new session goes to;
  // a scratch session's makes none (the next one gets a scratch folder of its own).
  useEffect(() => {
    if (!taskId || !state.cwd) return;
    if (scratchDay(state.cwd, scratchRoot(served))) pickProject("");
    else pickProject(projectFor(state.cwd, served ?? [])?.path ?? state.cwd);
  }, [taskId, state.cwd, served, pickProject]);

  const addProject = () => {
    if (!desk) return setPicking(true);
    desk.chooseProject().then((dir) => {
      if (!dir) return;
      pickProject(dir);
      open(null);
      void refreshProjects();
    }, fail);
  };
  const removeProject = (path: string) => {
    api.removeProject(path).then(() => {
      if (path === project) pickProject("");
      void refreshProjects();
    }, fail);
  };
  const projectName = (served && projectFor(project, served)?.name) || basename(project);
  // The listing, while the page is hidden less often (a desktop app's
  // notifications come from it, so it keeps looking).
  usePoll(refreshSessions, 3000, desk ? 10_000 : 60_000);

  useEffect(() => {
    api.models().then(setModels, fail);
  }, [api, fail]);

  useEffect(() => {
    api.commands(taskId).then((c) => setCommands([...PAGE_COMMANDS, ...c]), () => setCommands(PAGE_COMMANDS));
  }, [api, taskId]);

  // The sidebar's row of the open session follows its stream; the listing
  // is read again when the stream shows it changed.
  useEffect(() => {
    void refreshSessions();
  }, [taskId, state.busy, state.ended, refreshSessions]);

  // Cards are asked for only while the session waits on a person, or a
  // sub-agent (whose own approvals surface here) runs.
  const refreshPending = useCallback(() => {
    if (taskId) api.pending(taskId).then(setPending, fail);
  }, [api, taskId, fail]);
  const asking = state.waitingOn === "approval" || state.waitingOn === "question" || state.running.length > 0;
  // Asked again at once when the stream shows a new wait; sub-agents' own
  // approvals are not in it, so the cards are also polled (not while hidden).
  useEffect(() => {
    if (!asking) return setPending([]);
    refreshPending();
  }, [refreshPending, asking, state.waitingIds]);
  usePoll(refreshPending, asking ? 1500 : null, null);

  useEffect(() => {
    if (taskId && state.replied) api.usage(taskId).then(setUsage, () => undefined);
  }, [api, taskId, state.replied]);

  const findFiles = useCallback((q: string) => api.files(q, taskId), [api, taskId]);
  const recall = useCallback(() => store.get().items.flatMap((i) => (i.kind === "user" && i.text ? [i.text] : [])), [store]);

  const replace = (id: string) => {
    const next = new Set(replaced).add(id);
    saveReplaced(next);
    setReplaced(next);
  };

  const ended = state.ended !== null;

  /** The session a message goes to: the open one, a new one, or (the open
   * one has ended) a new one continuing its conversation. */
  const target = async (): Promise<string> => {
    if (taskId && !ended) return taskId;
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const config = taskId
      ? continuationConfig({ model: state.model, cwd: state.cwd }, timezone)
      : newSessionConfig({ mode: newMode, model: newModel, project }, timezone);
    const id = await api.create(null, config, "main", taskId ?? undefined);
    if (taskId) replace(taskId);
    open(id);
    return id;
  };

  const send = async (text: string, images: Attachment[]) => {
    setError("");
    setSent((n) => n + 1);
    const [name, ...rest] = text.startsWith("/") ? text.slice(1).split(" ") : [""];
    const arg = rest.join(" ").trim();
    try {
      if (name === "clear" || name === "new") return open(null);
      if (name === "model" && arg) return changeModel(arg);
      if (name === "rewind") return taskId ? setRewinding(true) : setError("Nothing to rewind to yet.");
      if (name === "plugins") return setPluginsOpen(true);
      if (name === "rename" && !taskId) return setError("Nothing to rename yet.");
      if (name === "rename" && !arg) return rename();
      if (name && (!taskId || ended)) {
        // A local command answers here: no session is made for it.
        const typed = await api.runCommand(text, taskId, !taskId ? project : undefined);
        if (typed?.local) return dispatch({ type: "notice", text: typed.text, tone: "info" });
      }
      // A first message takes the same path as every later one: the task
      // is made empty, then the message is sent (slash commands expand).
      const id = await target();
      if (name === "rename") {
        await api.signal(id, "rename", { title: arg });
        return void refreshSessions();
      }
      const input: Input = images.length
        ? { role: "user", content: [{ type: "text", text }, ...images.map((i) => ({ type: "image" as const, media_type: i.mediaType, data: i.data }))] }
        : text;
      const got = await api.send(id, input);
      if (id !== taskId) void refreshSessions();
      if (got.output !== undefined) dispatch({ type: "notice", text: got.output, tone: "info" });
      else if (got.inbox_seq !== undefined) dispatch({ type: "queued", seq: got.inbox_seq, text });
    } catch (e) {
      fail(e);
    }
  };

  const changeModel = (model: string) => {
    if (!taskId) return setNewModel(model);
    api.signal(taskId, "config", { model }).catch(fail);
  };
  // A new session's mode; a running one's is the permissions plugin's to show and switch.
  const cycleMode = () => {
    const modes = NEW_SESSION_MODES.map(([m]) => m);
    setNewMode(modes[(modes.indexOf(newMode) + 1) % modes.length]);
  };
  const rename = () => {
    if (!taskId) return;
    const named = window.prompt("Rename the session", title);
    if (named?.trim()) api.signal(taskId, "rename", { title: named.trim() }).then(refreshSessions, fail);
  };

  const window_ = models.find((m) => m.id === state.model)?.context_window ?? 0;
  return (
    <SlotsContext.Provider value={plugins.registry}>
    <div className={navOpen ? "layout nav-open" : "layout"}>
      <Sidebar
        sessions={sessions.filter((s) => !replaced.has(s.task_id))}
        projects={served}
        project={project}
        current={taskId}
        live={taskId && state.lastSeq ? { title, status: liveStatus(state) } : undefined}
        onOpen={open}
        onNew={() => open(null)}
        onSelectProject={(path) => {
          pickProject(path);
          open(null);
        }}
        onNewIn={(path) => {
          pickProject(path);
          open(null);
        }}
        onAddProject={addProject}
        onRemoveProject={removeProject}
        archived={showArchived}
        onShowArchived={(show) => {
          listed.current = null; // another list: nothing in it is news
          setShowArchived(show);
        }}
        onArchive={(id, archived) => void api.archive(id, archived).then(refreshSessions, fail)}
        onDisconnect={onDisconnect}
      />
      <div className="scrim" onClick={() => setNavOpen(false)} />
      <main className="main">
        <header className="header">
          <button className="icon-button nav-toggle" aria-label="Sessions" onClick={() => setNavOpen(!navOpen)}>
            ☰
          </button>
          <div className="heading">
            <Slot name={SLOTS.sessionHeader} props={{ title }}>
              <button className="title" onClick={rename} title="Rename" disabled={!taskId}>
                {taskId ? title || "Untitled session" : "New session"}
              </button>
            </Slot>
            {state.cwd && (
              <span className="cwd muted" title={state.cwd}>
                {shortPath(state.cwd)}
              </span>
            )}
          </div>
          <UsageBadge usage={usage} contextTokens={state.contextTokens} window={window_} />
          {taskId && (
            <button className="icon-button" title="Rewind" aria-label="Rewind" onClick={() => setRewinding(true)}>
              ↶
            </button>
          )}
          <button className="icon-button" aria-label="Plugins" title="Plugins" onClick={() => setPluginsOpen(true)}>
            ⧉
          </button>
          <button className="icon-button" aria-label="Toggle theme" title="Toggle theme" onClick={toggleTheme}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </header>
        <div className="body">
          <FollowScroll resetKey={`${taskId ?? ""}#${sent}`}>
            {!taskId && (
              <div className="welcome">
                <h1>What should we work on?</h1>
                {project ? (
                  <>
                    <div className="welcome-project" title={project}>
                      <span className="welcome-project-name">{projectName}</span>
                      <span className="muted welcome-project-path">{project}</span>
                    </div>
                    <button className="link welcome-no-project" onClick={() => pickProject("")}>
                      Use a scratch folder instead
                    </button>
                  </>
                ) : (
                  <p className="muted welcome-scratch">
                    No project: this session gets its own scratch folder.{" "}
                    <button className="link" onClick={addProject}>
                      Add a project
                    </button>{" "}
                    to work in one of yours.
                  </p>
                )}
              </div>
            )}
            <LiveTranscript key={taskId ?? ""} store={store} />
            <PendingCards pending={pending} api={api} answered={refreshPending} />
          </FollowScroll>
          <aside className="side-panels" aria-label="Plugin panels">
            <Slot name={SLOTS.sidebar} />
          </aside>
        </div>
        {error && (
          <div className="notice error banner" onClick={() => setError("")}>
            {error}
          </div>
        )}
        <footer className="footer">
          <Notices items={uiItems} />
          <Widgets items={uiItems} placement="above" />
          <Slot name={SLOTS.composer}>
          <Composer
            busy={state.busy}
            commands={commands}
            findFiles={findFiles}
            onSend={send}
            onStop={() => taskId && api.cancel(taskId, true).catch(fail)}
            onCycleMode={taskId ? undefined : cycleMode}
            placeholder={ended ? "This session has ended. Send a message to continue it." : undefined}
            draft={draft}
            keep={composerKeep}
            recall={recall}
          />
          </Slot>
          <Widgets items={uiItems} placement="below" />
          <div className="pickers">
            {!taskId && <NewSessionMode mode={newMode} onChange={setNewMode} />}
            <StatusBar items={uiItems} context={{ tokens: state.contextTokens, window: window_ }}>
              <ModelPicker model={taskId ? state.model : newModel} models={models} onChange={changeModel} />
            </StatusBar>
          </div>
        </footer>
      </main>
      {rewinding && taskId && (
        <RewindDialog
          api={api}
          taskId={taskId}
          onClose={() => setRewinding(false)}
          onRewound={(id, prompt) => {
            setRewinding(false);
            if (id !== taskId) {
              // The conversation went back: its prompt is the person's to edit and send again.
              setDraft({ text: prompt, n: Date.now() });
              replace(taskId);
              open(id);
            }
            void refreshSessions();
          }}
        />
      )}
      {picking && (
        <FolderPicker
          api={api}
          start={project}
          onClose={() => setPicking(false)}
          onAdd={async (path) => {
            const added = await api.addProject(path);
            setPicking(false);
            pickProject(added.path);
            open(null);
            void refreshProjects();
          }}
        />
      )}
      {pluginsOpen && (
        <PluginsDialog
          api={api}
          listing={plugins.listing}
          refresh={async () => {
            await plugins.refresh();
            api.commands(taskId).then((c) => setCommands([...PAGE_COMMANDS, ...c]), () => undefined);
          }}
          taskId={taskId && !ended ? taskId : null}
          taskOff={state.pluginsOff}
          status={plugins.status}
          registry={plugins.registry}
          onClose={() => setPluginsOpen(false)}
        />
      )}
      <div hidden>
        <Slot name={SLOTS.theme} />
      </div>
    </div>
    </SlotsContext.Provider>
  );
}
