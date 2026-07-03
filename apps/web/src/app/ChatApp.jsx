import {
  Bot,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RefreshCw,
  Sun,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Conversation,
  ConversationContent,
} from "../components/ai-elements/conversation.jsx";
import {
  PromptInputProvider,
} from "../components/ai-elements/prompt-input.jsx";
import { Lightbox } from "../components/Lightbox.jsx";
import { ToastStack } from "../components/Toast.jsx";
import {
  mergeComposerPref,
  mergePanelPref,
  PANEL_WIDTH_MAX,
  PANEL_WIDTH_MIN,
  storedComposerPref,
  storedPanelPref,
} from "../lib/composer-prefs.js";
import { useThemeToggle } from "../lib/theme.js";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { appReloadHitSeq, latestOpenApp } from "./app-preview.js";
import { useChatData } from "./chat-data.js";
import { ChatComposer, LandingHints } from "./ChatComposer.jsx";
import { ChatHeader } from "./ChatHeader.jsx";
import { NewProjectControl, SessionList } from "./ChatSidebar.jsx";
import { BackgroundJobModal, SubtaskModal } from "./ChatModals.jsx";
import { RightDock } from "./RightDock.jsx";
import {
  ApprovalGroup,
  BackgroundJobsStrip,
  QuestionPrompt,
  ResponseIndicator,
  RunningStrip,
  TodoStrip,
  Transcript,
} from "./Transcript.jsx";

const MAIN_AGENT = "main";
const SIDEBAR_WIDTH = 264;
const SIDEBAR_COLLAPSED_WIDTH = 44;
const PANEL_RESIZER_WIDTH = 6;
const CHAT_MIN_WIDTH_WITH_PANEL = 320;
const PANEL_PUSH_BREAKPOINT = 920;
function maxPanelWidthForViewport(viewportWidth, sidebarCollapsed) {
  const width = Number(viewportWidth);
  if (!Number.isFinite(width)) return PANEL_WIDTH_MAX;
  if (width <= PANEL_PUSH_BREAKPOINT) return PANEL_WIDTH_MAX;
  const sidebar = sidebarCollapsed ? SIDEBAR_COLLAPSED_WIDTH : SIDEBAR_WIDTH;
  const available =
    width - sidebar - PANEL_RESIZER_WIDTH - CHAT_MIN_WIDTH_WITH_PANEL;
  return Math.max(PANEL_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, available));
}

function ChatApp() {
  const chat = useChatData();
  const { theme, toggleTheme } = useThemeToggle();
  // The composer seeds from the LAST-USED prefs (localStorage) so a fresh
  // session opens with what you picked last time — no re-picking every time.
  // Switching INTO an existing session re-hydrates these from that session's
  // own bound config (the session-switch effect below); only manual picks
  // write back to prefs.
  const initialPref = storedComposerPref();
  const [permission, setPermission] = useState(
    () => initialPref.permission || "default",
  );
  // codex addendum (workspace-and-session-path.md): per-turn model + effort and
  // the create-once workspace binding, all seeded from last-used prefs. model /
  // effort are editable on any turn; workspace is only choosable while starting a
  // NEW session (an existing session shows its durable binding read-only).
  const [model, setModel] = useState(() => initialPref.model || "");
  const [effort, setEffort] = useState(() => initialPref.effort || "");
  const [selectedWorkspace, setSelectedWorkspace] = useState(
    () => initialPref.workspace || "",
  );
  const [composerText, setComposerText] = useState("");
  // the set of enabled MCP server aliases for the next turn (a
  // Set of alias strings). The request body carries just these aliases — never
  // a url/token (those live host-side). Starts empty (no MCP).
  const [enabledMcp, setEnabledMcp] = useState(() => new Set());
  // The background-shell job whose output drill-in is open, or
  // null. Local UI state — independent of the subtask popup so the two never
  // collide.
  const [openJob, setOpenJob] = useState(null);

  // the shared lightbox's current zoomed-in
  // src (a URL, source-agnostic); null = closed. Chat-bubble thumbnails open it,
  // and the file panel's image preview reuses the same Lightbox.
  const [lightboxSrc, setLightboxSrc] = useState(null);

  // the right-dock file panel. Open state + width persist to
  // localStorage (panel prefs), so the panel reopens at the size you left it and
  // stays put across a refresh. `panelType` was the generic-shell hook — v1
  // shipped "files"-only; a later revision added
  // two tabs, `Files | App`, and the selected tab also persists in the panel
  // prefs (defaulting to "files").
  const storedPanel = storedPanelPref();
  const [panelOpen, setPanelOpen] = useState(() => storedPanel.open);
  const [panelWidth, setPanelWidth] = useState(() => storedPanel.width);
  const [panelType, setPanelType] = useState(() => storedPanel.panelType);
  // Bumped to force the panel to re-fetch its tree (manual refresh / switch).
  const [panelRefreshKey, setPanelRefreshKey] = useState(0);

  // the "App" tab's state. The
  // model calls open_app → the backend emits {type:"open_app", url, dir} in that
  // ToolResultRecorded's side_effects (observed to reach the front-end via the
  // event stream). The front-end records the current app's url / mount dir here;
  // the app follows the session (reset on session switch — see the hydration
  // block below).
  //   - appUrl: the iframe src (the gateway's same-origin preview address).
  //     null = no app opened yet.
  //   - appDir: the mount dir (used for prefix-match change detection).
  //   - appReloadKey: bump it to force the iframe to remount = reload (live
  //     preview after editing files under app/).
  const [appUrl, setAppUrl] = useState(null);
  const [appDir, setAppDir] = useState("");
  const [appReloadKey, setAppReloadKey] = useState(0);
  // The seq of the latest open_app the panel already opened on, and the seq of
  // the change hit the iframe already reloaded on. Both throttle by acting ONLY
  // when the seq actually advances, so not every event switches the tab /
  // reloads the iframe.
  const lastOpenAppSeqRef = useRef(-1);
  const lastAppReloadSeqRef = useRef(-1);

  const selectPanelType = useCallback((next) => {
    setPanelType(next);
    mergePanelPref({ panelType: next });
  }, []);

  // The left session sidebar's collapsed state, persisted in the panel prefs.
  // When collapsed the first column shrinks to a 44px rail with just an expand
  // button, ceding horizontal space to the chat area.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => !!storedPanel.sidebar,
  );
  const toggleSidebar = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      mergePanelPref({ sidebar: next });
      return next;
    });
  };

  const togglePanel = () => {
    setPanelOpen((prev) => {
      const next = !prev;
      mergePanelPref({ open: next });
      // Re-fetch the tree on each open so a panel opened mid-session shows the
      // current files (D6: open / switch re-fetches).
      if (next) setPanelRefreshKey((k) => k + 1);
      return next;
    });
  };
  const closePanel = () => {
    setPanelOpen(false);
    mergePanelPref({ open: false });
  };

  // Pointer-drag the divider to resize the panel; clamp to [MIN, MAX] and
  // persist on release. The panel sits on the RIGHT, so dragging left (smaller
  // clientX) widens it — width = viewport right edge − pointer x.
  const panelDragRef = useRef(null);
  const onPanelResizeStart = (event) => {
    event.preventDefault();
    panelDragRef.current = true;
    // P5 — pointermove fires far faster than the display refreshes; writing
    // --panel-w (a grid track that reflows the chat + the app iframe) on every
    // event thrashes layout. Coalesce into ONE setPanelWidth per frame via rAF,
    // keeping only the latest pointer position.
    let raf = 0;
    let latest = null;
    const onMove = (moveEvent) => {
      if (!panelDragRef.current) return;
      const raw = window.innerWidth - moveEvent.clientX;
      const max = maxPanelWidthForViewport(window.innerWidth, sidebarCollapsed);
      latest = Math.min(max, Math.max(PANEL_WIDTH_MIN, raw));
      if (raf) return;
      raf = window.requestAnimationFrame(() => {
        raf = 0;
        if (latest != null) setPanelWidth(latest);
      });
    };
    const onUp = () => {
      panelDragRef.current = false;
      if (raf) window.cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      setPanelWidth((w) => {
        const finalWidth = latest != null ? latest : w;
        mergePanelPref({ width: finalWidth });
        return finalWidth;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const panelMounted = panelOpen && !!chat.activeTaskId;

  useEffect(() => {
    if (!panelMounted) return;
    setPanelWidth((width) => {
      const max = maxPanelWidthForViewport(window.innerWidth, sidebarCollapsed);
      const clamped = Math.min(max, Math.max(PANEL_WIDTH_MIN, width));
      if (clamped !== width) mergePanelPref({ width: clamped });
      return clamped;
    });
  }, [panelMounted, sidebarCollapsed]);

  // when the model calls
  // open_app and a newer open_app side-effect appears in the event stream (seq
  // larger than recorded): auto-open the panel + switch to the "App" tab + point
  // the iframe at the new url. Throttled by seq — one open_app fires the switch
  // only once across re-renders / merged events, after which the user can switch
  // back to "Files" without being yanked back to "App".
  useEffect(() => {
    const latest = latestOpenApp(chat.events);
    if (!latest || latest.seq <= lastOpenAppSeqRef.current) return;
    lastOpenAppSeqRef.current = latest.seq;
    // single-port revision:
    // url is a relative path `/preview/<token>/` resolved by the browser against
    // noeta's origin — so the iframe always uses the same host:port you reached
    // noeta on, reachable under VM / port-forward / tunnel.
    setAppUrl(latest.url);
    setAppDir(latest.dir || "");
    // New app opened → reset the reload baseline to the latest change to that dir
    // in the current event stream, so only changes arriving AFTER open_app
    // trigger a reload and the just-mounted iframe isn't pointlessly reloaded.
    lastAppReloadSeqRef.current = appReloadHitSeq(chat.events, latest.dir || "");
    setPanelOpen(true);
    mergePanelPref({ open: true });
    selectPanelType("app");
  }, [chat.events, selectPanelType]);

  // when the model edits files
  // under the app/ dir (reusing the same change detection) and the hit seq
  // advances, bump the iframe key to force a remount = reload (live preview).
  // Reloads only when the seq actually advances (throttle: multiple changes in
  // one turn each advance the seq, but only "newer than the last reload" acts,
  // not every event). Skipped when no app has been opened (appDir empty).
  useEffect(() => {
    if (!appDir) return;
    const hit = appReloadHitSeq(chat.events, appDir);
    if (hit > lastAppReloadSeqRef.current) {
      lastAppReloadSeqRef.current = hit;
      setAppReloadKey((k) => k + 1);
    }
  }, [chat.events, appDir]);

  // the app follows the
  // session: on session switch (activeTaskId changes), reset the "App" tab state,
  // else the previous session's iframe url bleeds into the new one. The seq
  // baseline is reset too (the new session's event stream starts fresh). Tab
  // selection itself is a UI preference, not cleared per session, so panelType is
  // not reset here.
  useEffect(() => {
    setAppUrl(null);
    setAppDir("");
    setAppReloadKey(0);
    lastOpenAppSeqRef.current = -1;
    lastAppReloadSeqRef.current = -1;
  }, [chat.activeTaskId]);

  // Permission mode is the composer's one backend-backed selector. A manual pick
  // updates the in-memory state AND remembers itself as the last-used default;
  // the session-switch effect below restores that pref directly.
  const onPermissionChange = (next) => {
    setPermission(next);
    mergeComposerPref({ permission: next });
  };
  // model / effort are per-turn knobs; a manual pick updates the in-memory state
  // AND remembers itself as the new-session default. The modelTouchedRef stops
  // the async "reflect the bound model" effect below from overwriting a pick.
  const modelTouchedRef = useRef(false);
  const onModelChange = (next) => {
    modelTouchedRef.current = true;
    setModel(next);
    mergeComposerPref({ model: next });
  };
  const onEffortChange = (next) => {
    setEffort(next);
    mergeComposerPref({ effort: next });
  };
  // workspace is the create-once durable binding, so it is only editable while
  // starting a new session; the pick is remembered as the next new-session
  // default.
  const onWorkspaceChange = (next) => {
    setSelectedWorkspace(next);
    mergeComposerPref({ workspace: next });
  };

  // Session-switch hydration: keyed on activeTaskId so it fires once per real
  // switch. Permission + effort are per-turn / non-durable, so every session
  // restores the last-used pref. For a NEW session, model + workspace also
  // restore from prefs (the user's last picks). For an EXISTING session, model
  // is left blank here and refined to the session's bound model by the effect
  // below as the stream resolves it; workspace is read-only (its durable
  // binding), shown from the session row.
  const hydratedTaskRef = useRef(undefined);
  useEffect(() => {
    const tid = chat.activeTaskId;
    if (tid === hydratedTaskRef.current) return;
    hydratedTaskRef.current = tid;
    const pref = storedComposerPref();
    setPermission(pref.permission || "default");
    setEffort(pref.effort || "");
    if (tid === null) {
      modelTouchedRef.current = true; // new session uses the pref pick as-is
      setModel(pref.model || "");
      setSelectedWorkspace(pref.workspace || "");
    } else {
      modelTouchedRef.current = false; // reflect the session's bound model
      setModel("");
    }
  }, [chat.activeTaskId]);

  // EXISTING session: reflect the durable bound model (chat.vm.model) in the
  // composer's model chip as it resolves from the stream — until the user makes
  // a manual per-turn pick (modelTouchedRef).
  useEffect(() => {
    if (chat.activeTaskId && !modelTouchedRef.current && chat.vm.model) {
      setModel(chat.vm.model);
    }
  }, [chat.activeTaskId, chat.vm.model]);

  // NEW-session default workspace: once /capabilities lands, seed the workspace
  // picker to the is_default project unless the user already has a valid
  // remembered / manual pick. Also recovers from a stale remembered id (a
  // project deleted since) by falling back to the default. Only runs on the
  // landing (no active session); waits until workspaces are actually loaded so a
  // momentary empty list never clobbers a good pick.
  useEffect(() => {
    if (chat.activeTaskId) return;
    const list = chat.options.workspaces || [];
    if (!list.length) return;
    const known = selectedWorkspace && list.some((ws) => ws && ws.id === selectedWorkspace);
    if (known) return;
    const def = list.find((ws) => ws && ws.is_default) || list[0];
    if (def && def.id) setSelectedWorkspace(def.id);
  }, [chat.activeTaskId, selectedWorkspace, chat.options.workspaces]);

  const questions = pendingQuestionsFromDetail(chat.activeDetail);
  const working =
    chat.openingSession ||
    isAgentWorking(chat.activeDetail, chat.vm, chat.activeTaskId);
  // One bottom-left "assistant is composing" affordance for every busy state,
  // the way mainstream chat UIs do it — not a top-centred status banner.
  // "Sending..." and the opening wait read as the agent composing, so they
  // show the bare typing animation; lifecycle ops (Approving/Closing/…) keep
  // their verb so the brief round-trip stays legible.
  const responding = working || !!chat.busyLabel;
  // A live transient-retry backoff (rate limit / flaky transport) labels the
  // composing indicator so a multi-second stall reads as "retrying", not as
  // the agent silently hanging. Lifecycle busy verbs still take precedence.
  const llmRetry = chat.vm?.llmRetry || null;
  const indicatorLabel =
    chat.busyLabel && chat.busyLabel !== "Sending..."
      ? chat.busyLabel
      : llmRetry
        ? `Provider error — retrying (${llmRetry.attempt}/${llmRetry.maxRetries})`
        : null;

  // The landing screen: a fresh, unsent session. The moment a goal is sent the
  // session is "opening" (task_id pending) or has a pendingGoalText, which flips
  // us into the normal transcript layout — so submitting auto-switches views.
  const landing = !chat.activeTaskId && !chat.openingSession && !chat.pendingGoalText;

  // The durable workspace bound to the active session (read-only display in the
  // composer / chip). The backend resolves the registry name from the welded
  // path (basename fallback); null = the shared default bucket.
  const activeRow = chat.sessions.find(
    (row) => row && row.task_id === chat.activeTaskId,
  );
  const boundWorkspaceName = activeRow?.workspace_name || "";

  // U1 — landing helper: fill the composer textarea from an example-prompt chip
  // (reuse the controlled composerText + focus the textarea). Landing-only.
  const fillComposer = useCallback((text) => {
    setComposerText(text);
    requestAnimationFrame(() => {
      const textarea = document.querySelector(".composer .ai-prompt-textarea");
      textarea?.focus();
      const len = textarea?.value.length || 0;
      textarea?.setSelectionRange(len, len);
    });
  }, []);

  const composer = (
    <div className="composer-dock">
      {/* U12 / P1 — the toast stack OVERLAYS above the composer (position:absolute,
          bottom:100%, no in-flow height), so a toast never pushes the composer
          down on the landing hero (grid) nor overlaps it in an active chat. */}
      <ToastStack toasts={chat.toasts} onDismiss={chat.dismissToast} />
      <PromptInputProvider value={composerText} onValueChange={setComposerText}>
      <ChatComposer
        autoFocus={landing}
        canSend={chat.status.canSend}
        commandIn={chat.commandIn}
        isNewSession={!chat.activeTaskId}
        onPermissionChange={onPermissionChange}
        onModelChange={onModelChange}
        onEffortChange={onEffortChange}
        onWorkspaceChange={onWorkspaceChange}
        onSubmit={(goal, images) =>
          chat.submitGoal({
            goal,
            agent: MAIN_AGENT,
            permission,
            enabledMcp: Array.from(enabledMcp),
            images,
            // Send the model only when it is an actual switch — i.e. it differs
            // from the session's currently-bound model (empty on a brand-new
            // session, so the opening pick always sends). Echoing the bound model
            // every follow-up turn would write a redundant ModelBound (and could
            // trip the selector allowlist on a single-model deployment).
            model: model && model !== chat.vm.model ? model : undefined,
            effort,
            // workspace is the create-once binding; submitGoal applies it only on
            // the opening POST /tasks and ignores it on a follow-up turn.
            workspace: selectedWorkspace,
          })
        }
        onCreateMcpServer={chat.createMcpServer}
        onUpdateMcpServer={chat.updateMcpServer}
        onDeleteMcpServer={chat.deleteMcpServer}
        onDiscoverMcpTools={chat.discoverMcpServerTools}
        onSetMcpTools={chat.setMcpServerTools}
        mcpServers={chat.options.mcpServers || []}
        enabledMcp={enabledMcp}
        onToggleMcp={(alias) =>
          setEnabledMcp((prev) => {
            const next = new Set(prev);
            if (next.has(alias)) next.delete(alias);
            else next.add(alias);
            return next;
          })
        }
        options={chat.options}
        permission={permission}
        model={model}
        effort={effort}
        selectedWorkspace={selectedWorkspace}
        boundWorkspaceName={boundWorkspaceName}
        onNotice={chat.showNotice}
        working={working}
        onStop={chat.cancelSession}
      />
      </PromptInputProvider>
    </div>
  );

  return (
    <div
      className={`app-shell${panelMounted ? " has-panel" : ""}${
        sidebarCollapsed ? " sidebar-collapsed" : ""
      }`}
      style={panelMounted ? { "--panel-w": `${panelWidth}px` } : undefined}
    >
      <aside className="session-sidebar" aria-label="Sessions">
        <header className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">
            <Bot size={ICON_LG} />
          </span>
          <h1>Noeta</h1>
          <span
            className={`connection-status connection-status--compact ${chat.status.connection.className}`}
            title={chat.status.connection.label}
            aria-label={`connection ${chat.status.connection.label}`}
          />
          <button
            className="icon-button sidebar-collapse-btn"
            type="button"
            onClick={toggleSidebar}
            title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {sidebarCollapsed ? (
              <PanelLeftOpen size={ICON_LG} />
            ) : (
              <PanelLeftClose size={ICON_LG} />
            )}
          </button>
          <button className="icon-button" type="button" onClick={toggleTheme}>
            {theme === "dark" ? <Sun size={ICON_LG} /> : <Moon size={ICON_LG} />}
          </button>
        </header>

        <button
          className="new-session-btn"
          type="button"
          disabled={!chat.commandIn}
          onClick={chat.newSession}
        >
          <Plus size={ICON_LG} />
          New session
        </button>

        {/* codex addendum: register a project (workspace) by absolute path; on
            success it shows up as a grouped bucket in the session list below. */}
        <NewProjectControl
          onCreate={chat.createWorkspace}
          disabled={!chat.commandIn}
        />

        <div className="sidebar-head">
          <h2>Sessions</h2>
          <button type="button" onClick={chat.loadTaskList}>
            <RefreshCw size={ICON_SM} />
          </button>
        </div>
        <SessionList
          activeTaskId={chat.activeTaskId}
          rows={chat.sessions.filter((row) => !row.parent_task_id)}
          workspaces={chat.options.workspaces}
          onSelect={chat.selectTask}
          onCancel={chat.cancelSession}
          onDelete={chat.deleteSession}
        />
      </aside>

      <main className={`chat-main${landing ? " is-landing" : ""}`}>
        {landing ? (
          <div className="chat-hero">
            <div className="chat-hero__head">
              <h1>What should we get done?</h1>
            </div>
            {composer}
            <LandingHints onFill={fillComposer} />
          </div>
        ) : (
          <>
            <ChatHeader
              activeDetail={chat.activeDetail}
              activeTaskId={chat.activeTaskId}
              commandIn={chat.commandIn}
              model={chat.vm.model}
              onReopen={chat.reopenSession}
              openingSession={chat.openingSession}
              panelOpen={panelOpen}
              onTogglePanel={togglePanel}
              pushToast={chat.pushToast}
              vm={chat.vm}
            />

            <Conversation className="chat-conversation">
              <ConversationContent resetKey={chat.activeTaskId}>
                <Transcript
                  activeTaskId={chat.activeTaskId}
                  events={chat.events}
                  ensureMessageBodiesBatch={chat.ensureMessageBodiesBatch}
                  ensureThinkingBatch={chat.ensureThinkingBatch}
                  localImageCache={chat.localImageCache}
                  onOpenImage={setLightboxSrc}
                  messageFullCache={chat.messageFullCache}
                  messageTextCache={chat.messageTextCache}
                  onOpenSubtask={chat.openSubtask}
                  pendingGoalText={chat.pendingGoalText}
                  responseThinkingCache={chat.responseThinkingCache}
                  vm={chat.vm}
                />
                {chat.vm.pendingApprovals.length ? (
                  <ApprovalGroup
                    approvals={chat.vm.pendingApprovals}
                    hidden={chat.hiddenApprovals}
                    onResolve={chat.resolveApproval}
                    onDefer={chat.deferApprovalResolve}
                  />
                ) : null}
                {questions.map((question) => (
                  <QuestionPrompt
                    key={question.question_id || question.reason}
                    pending={question}
                    onSubmit={chat.submitQuestionAnswer}
                  />
                ))}
                {responding ? <ResponseIndicator label={indicatorLabel} /> : null}
              </ConversationContent>
            </Conversation>

            {chat.activeTaskId ? (
              <>
                <TodoStrip vm={chat.vm} />
                <BackgroundJobsStrip
                  jobs={chat.backgroundJobs}
                  onOpen={(job) => setOpenJob(job.jobId)}
                />
                <RunningStrip vm={chat.vm} onOpen={chat.openSubtask} />
              </>
            ) : null}

            {composer}
          </>
        )}
      </main>

      {panelMounted ? (
        <RightDock
          activeTaskId={chat.activeTaskId}
          appReloadKey={appReloadKey}
          appUrl={appUrl}
          discoverTaskFiles={chat.discoverTaskFiles}
          events={chat.events}
          onClose={closePanel}
          onOpenImage={setLightboxSrc}
          onPanelResizeStart={onPanelResizeStart}
          onSelectPanelType={selectPanelType}
          panelRefreshKey={panelRefreshKey}
          panelType={panelType}
          readTaskFile={chat.readTaskFile}
          working={working}
        />
      ) : null}

      {openJob
        ? (() => {
            // Look the job up live so a status/ref change folded while the modal
            // is open (e.g. the process exits) reflects without a re-click; drop
            // the modal if the jobId somehow disappears.
            const job = chat.backgroundJobs.find((j) => j.jobId === openJob);
            return job ? (
              <BackgroundJobModal
                job={job}
                taskId={chat.activeTaskId}
                onClose={() => setOpenJob(null)}
              />
            ) : null;
          })()
        : null}

      {chat.popupStack.length ? (
        <SubtaskModal
          detail={chat.popupDetail}
          ensureMessageBodiesBatch={chat.ensureMessageBodiesBatch}
          ensureThinkingBatch={chat.ensureThinkingBatch}
          events={chat.popupEvents}
          localImageCache={chat.localImageCache}
          onOpenImage={setLightboxSrc}
          messageFullCache={chat.messageFullCache}
          messageTextCache={chat.messageTextCache}
          onClose={chat.closeSubtask}
          onCrumb={chat.gotoBreadcrumb}
          onDrill={chat.drillSubtask}
          responseThinkingCache={chat.responseThinkingCache}
          stack={chat.popupStack}
          vm={chat.popupVm}
        />
      ) : null}

      {/* shared lightbox: click a chat-bubble thumbnail to zoom. Not rendered when src=null. */}
      <Lightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
    </div>
  );
}

function pendingQuestionsFromDetail(detail) {
  if (!detail || detail.wake_kind !== "question") return [];
  if (!Array.isArray(detail.pending_questions)) return [];
  if (!detail.question_id) return detail.pending_questions;
  return detail.pending_questions.filter(
    (question) => question.question_id === detail.question_id,
  );
}

function isAgentWorking(detail, vm, activeTaskId) {
  if (!activeTaskId) return false;
  if (detail?.approval_call_id || detail?.question_id) return false;
  if (vm?.pendingApprovals?.length) return false;
  if (detail?.wake_kind === "next-goal" || detail?.closed) return false;
  const status = String(detail?.status_text || detail?.status || vm?.status || "");
  return status.toLowerCase().includes("run");
}

export { ChatApp };
