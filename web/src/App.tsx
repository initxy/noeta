import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Api, ApiError, initialConnection, saveConnection, type Connection } from "./api/client";
import type { Command, Input, ModelInfo, Pending, TaskSummary, TreeUsage } from "./api/types";
import { Composer, type Attachment } from "./components/Composer";
import { ModePicker, ModelPicker, RewindDialog, UsageBadge } from "./components/Controls";
import { PendingCards } from "./components/PendingCard";
import { Sidebar } from "./components/Sidebar";
import { TodoPanel } from "./components/TodoPanel";
import { Transcript } from "./components/Transcript";
import { attentionChanges, desktop } from "./desktop";
import { ApiContext, useTask } from "./state/useTask";

/** Commands the page runs itself, as Claude Code's app does. */
const PAGE_COMMANDS: Command[] = [
  { name: "clear", description: "Start a new session", argument_hint: "", local: true },
  { name: "rename", description: "Rename this session", argument_hint: "<title>", local: true },
  { name: "model", description: "Switch the model", argument_hint: "<model>", local: true },
  { name: "rewind", description: "Go back to before a prompt", argument_hint: "", local: true },
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
      />
    </ApiContext.Provider>
  );
}

function Workspace({ api, theme, toggleTheme, onUnauthorized }: { api: Api; theme: string; toggleTheme: () => void; onUnauthorized: (m: string) => void }) {
  const [sessions, setSessions] = useState<TaskSummary[]>([]);
  const [taskId, setTaskId] = useState<string | null>(sessionFromHash);
  const [state, dispatch] = useTask(api, taskId);
  const [pending, setPending] = useState<Pending[]>([]);
  const [commands, setCommands] = useState<Command[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [usage, setUsage] = useState<TreeUsage | null>(null);
  const [newMode, setNewMode] = useState("default");
  const [newModel, setNewModel] = useState("");
  const [rewinding, setRewinding] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [error, setError] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const desk = desktop();
  const [project, setProject] = useState("");
  const listed = useRef<TaskSummary[] | null>(null);
  const pinned = useRef(true); // the view follows new output while it is at the bottom

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [state.items, state.live, state.queued, pending]);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onUnauthorized("The server refused the token.");
      else setError(String(e instanceof Error ? e.message : e));
    },
    [onUnauthorized],
  );

  const open = useCallback((id: string | null) => {
    setTaskId(id);
    pinned.current = true;
    setPending([]);
    setUsage(null);
    setNavOpen(false);
    history.replaceState(null, "", id ? `#session=${id}` : window.location.pathname);
  }, []);

  const refreshSessions = useCallback(
    () =>
      api.summaries().then((list) => {
        if (desk && listed.current) for (const a of attentionChanges(listed.current, list)) desk.notify(a);
        listed.current = list;
        setSessions(list);
      }, fail),
    [api, fail, desk],
  );

  useEffect(() => {
    if (!desk) return;
    void desk.project().then(setProject);
    return desk.onCommand((c) => {
      if (c.name === "new-session") open(null);
      else if (c.name === "open-session") open(c.taskId);
      else setProject(c.path);
    });
  }, [desk, open]);
  useEffect(() => {
    void refreshSessions();
    const timer = setInterval(refreshSessions, 3000);
    return () => clearInterval(timer);
  }, [refreshSessions]);

  useEffect(() => {
    api.models().then(setModels, fail);
  }, [api, fail]);

  useEffect(() => {
    api.commands(taskId).then((c) => setCommands([...PAGE_COMMANDS, ...c]), () => setCommands(PAGE_COMMANDS));
  }, [api, taskId]);

  const refreshPending = useCallback(() => {
    if (taskId) api.pending(taskId).then(setPending, fail);
  }, [api, taskId, fail]);
  useEffect(() => {
    refreshPending();
    if (state.waitingOn !== "children" && !state.busy) return;
    const timer = setInterval(refreshPending, 1500);
    return () => clearInterval(timer);
  }, [refreshPending, state.waitingOn, state.busy, state.lastSeq]);

  useEffect(() => {
    if (taskId && state.replied) api.usage(taskId).then(setUsage, () => undefined);
  }, [api, taskId, state.replied]);

  const findFiles = useCallback((q: string) => api.files(q, taskId), [api, taskId]);

  const send = async (text: string, images: Attachment[]) => {
    setError("");
    const [name, ...rest] = text.startsWith("/") ? text.slice(1).split(" ") : [""];
    const arg = rest.join(" ").trim();
    try {
      if (name === "clear" || name === "new") return open(null);
      if (name === "rewind" && taskId) return setRewinding(true);
      if (name === "rename" && taskId) {
        await api.signal(taskId, "rename", { title: arg });
        return void refreshSessions();
      }
      if (name === "model" && arg) return changeModel(arg);
      const input: Input = images.length
        ? { role: "user", content: [{ type: "text", text }, ...images.map((i) => ({ type: "image" as const, media_type: i.mediaType, data: i.data }))] }
        : text;
      if (!taskId) {
        const config: Record<string, unknown> = { permission_mode: newMode, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone };
        if (newModel) config.model = newModel;
        if (desk && project) config.cwd = project;
        const id = await api.create(input, config);
        open(id);
        void refreshSessions();
        return;
      }
      const got = await api.send(taskId, input);
      if (got.output !== undefined) dispatch({ type: "notice", text: got.output, tone: "info" });
      else if (got.inbox_seq !== undefined) dispatch({ type: "queued", seq: got.inbox_seq, text });
    } catch (e) {
      fail(e);
    }
  };

  const changeMode = (mode: string) => {
    if (!taskId) return setNewMode(mode);
    api.signal(taskId, "permission_mode", { mode }).catch(fail);
  };
  const changeModel = (model: string) => {
    if (!taskId) return setNewModel(model);
    api.signal(taskId, "config", { model }).catch(fail);
  };
  const rename = () => {
    if (!taskId) return;
    const title = window.prompt("Rename the session", state.title);
    if (title?.trim()) api.signal(taskId, "rename", { title: title.trim() }).then(refreshSessions, fail);
  };

  const window_ = models.find((m) => m.id === state.model)?.context_window ?? 0;
  const ended = state.ended !== null;
  return (
    <div className={navOpen ? "layout nav-open" : "layout"}>
      <Sidebar sessions={sessions} current={taskId} onOpen={open} onNew={() => open(null)} />
      <div className="scrim" onClick={() => setNavOpen(false)} />
      <main className="main">
        <header className="header">
          <button className="icon-button nav-toggle" aria-label="Sessions" onClick={() => setNavOpen(!navOpen)}>
            ☰
          </button>
          <div className="heading">
            <button className="title" onClick={rename} title="Rename" disabled={!taskId}>
              {taskId ? state.title || "Untitled session" : "New session"}
            </button>
            {state.cwd && <span className="cwd muted">{state.cwd}</span>}
          </div>
          <UsageBadge usage={usage} contextTokens={state.contextTokens} window={window_} />
          {taskId && (
            <button className="icon-button" title="Rewind" aria-label="Rewind" onClick={() => setRewinding(true)}>
              ↶
            </button>
          )}
          <button className="icon-button" aria-label="Toggle theme" title="Toggle theme" onClick={toggleTheme}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </header>
        <div className="body">
          <div
            className="scroll"
            ref={scroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
            }}
          >
            {!taskId && (
              <div className="welcome">
                <h1>What should we work on?</h1>
                {desk ? (
                  <p className="muted">
                    A new session runs in{" "}
                    <button
                      className="project-pick"
                      title="Choose the project folder"
                      onClick={() => void desk.chooseProject().then((dir) => dir && setProject(dir))}
                    >
                      {project || "a project folder"}
                    </button>{" "}
                    with the mode and model below.
                  </p>
                ) : (
                  <p className="muted">A new session runs in the server's directory with the mode and model below.</p>
                )}
              </div>
            )}
            <Transcript state={state} />
            <PendingCards pending={pending} api={api} answered={refreshPending} />
          </div>
          <TodoPanel todos={state.todos} />
        </div>
        {error && (
          <div className="notice error banner" onClick={() => setError("")}>
            {error}
          </div>
        )}
        <footer className="footer">
          <Composer
            busy={state.busy}
            disabled={ended}
            commands={commands}
            findFiles={findFiles}
            onSend={send}
            onStop={() => taskId && api.cancel(taskId, true).catch(fail)}
            placeholder={ended ? "This session has ended" : undefined}
          />
          <div className="pickers">
            <ModePicker mode={taskId ? state.mode : newMode} modes={taskId ? state.modes : null} onChange={changeMode} />
            <ModelPicker model={taskId ? state.model : newModel} models={models} onChange={changeModel} />
          </div>
        </footer>
      </main>
      {rewinding && taskId && (
        <RewindDialog
          api={api}
          taskId={taskId}
          onClose={() => setRewinding(false)}
          onRewound={(id) => {
            setRewinding(false);
            open(id);
            void refreshSessions();
          }}
        />
      )}
    </div>
  );
}
