import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from './api/client'
import { channelsApi, modelsApi, sessionsApi, templatesApi } from './api/endpoints'
import type {
  AdvancePreview,
  AnswerPayload,
  Channel,
  ChannelTopic,
  ImageAttachment,
  ModelInfo,
  Session,
  Template,
  WorkflowTemplate,
  WorkflowView,
} from './api/types'
import { pendingQuestion, useChat } from './chat/useChat'
import { renderBlocks } from './chat/streaming'
import { BoardPage } from './components/BoardPage'
import { ChannelPage } from './components/ChannelPage'
import { Composer } from './components/Composer'
import { Conversation } from './components/Conversation'
import { TopicPanel } from './components/TopicPanel'
import { TodoStrip } from './components/TodoStrip'
import { IconCopy, IconPlus, IconSidebar, IconTrace } from './components/icons'
import { LoginPage } from './components/LoginPage'
import { Logo } from './components/Logo'
import { ReconnectBanner } from './components/ReconnectBanner'
import { Sidebar, type MainView } from './components/Sidebar'
import { SidePanel, useSidePanel } from './components/SidePanel'
import { SkillsPage } from './components/SkillsPage'
import { TemplatesPage } from './components/TemplatesPage'
import { KnowledgePage } from './components/KnowledgePage'
import { FeedbackPage } from './components/FeedbackPage'
import { MemoriesPage } from './components/MemoriesPage'
import { AdvanceDialog } from './components/workflow/AdvanceDialog'
import { StartTemplateModal } from './components/workflow/StartTemplateModal'
import { TaskTabs } from './components/workflow/TaskTabs'
import { AdminPage } from './components/admin/AdminPage'
import { UserSettingsPage } from './components/UserSettingsPage'
import { cn } from './lib/cn'
import { copyText } from './lib/clipboard'
import { dataUrlFromAttachment } from './lib/imageAttach'
import { useAuth } from './state/auth'
import { renderPrompt } from './lib/templatePrompt'
import { resolveModelPref, writeModelPref } from './state/modelPref'
import { SpaceProvider, useSpace } from './state/space'
import { useToast } from './state/toast'

export default function App() {
  const { user, checked } = useAuth()
  if (!checked) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="rail-dot rail-dot--active" />
      </div>
    )
  }
  if (!user) return <LoginPage />
  return (
    <SpaceProvider>
      <Workbench />
    </SpaceProvider>
  )
}

function Workbench() {
  const { toast } = useToast()
  const { user } = useAuth()
  const { currentSpace, currentSpaceId } = useSpace()
  const isSpaceOwner = currentSpace?.my_role === 'owner'

  // ---- Session list ----
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [activeId, setActiveId] = useState<string | null>(null)
  // Empty sessions created by this client: enter the hero empty state without waiting for replay_done.
  const [knownEmpty, setKnownEmpty] = useState<Set<string>>(() => new Set())

  // ---- Models ----
  const [models, setModels] = useState<ModelInfo[]>([])
  const [modelBySession, setModelBySession] = useState<Record<string, string>>({})
  // Per-session effort choice (falls back to the current model's default_effort when unset).
  const [effortBySession, setEffortBySession] = useState<Record<string, string>>({})
  // model/effort choice in the hero empty state (activeId===null, draft not yet
  // persisted); written to the session with the first sendMessage, after which the
  // per-session maps take over.
  const [draftModel, setDraftModel] = useState<string>('')
  const [draftEffort, setDraftEffort] = useState<string>('')

  // ---- Layout ----
  const [view, setView] = useState<MainView>('chat')
  const [adminTraceSessionId, setAdminTraceSessionId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(
    () => window.innerWidth >= 1024,
  )
  const panel = useSidePanel()

  // ---- Channels / board (space collaboration layer, ADR-0016) ----
  // Off for now: the feature is not polished; hidden in team spaces too. When ready,
  // switch back to enabling by space type.
  const showCollab = false  // !!currentSpace && !currentSpace.is_personal
  const [channels, setChannels] = useState<Channel[]>([])
  const [activeChannelId, setActiveChannelId] = useState<string | null>(null)
  // The topic panel currently open (right rail while view==='channel').
  const [openTopic, setOpenTopic] = useState<ChannelTopic | null>(null)
  // The topic to auto-open after a board backlink jumps into a channel.
  const [focusTopicId, setFocusTopicId] = useState<string | null>(null)
  const activeChannel = channels.find((c) => c.id === activeChannelId) ?? null

  const reloadChannels = useCallback(() => {
    if (!currentSpaceId || !showCollab) return
    channelsApi
      .list(currentSpaceId)
      .then((r) => setChannels(r.channels))
      .catch(() => {
        /* Unread polling failures are silent; try again next round. */
      })
  }, [currentSpaceId, showCollab])

  // Space switch: reset channel state and fetch the list; a 30s unread-badge poll as
  // the fallback (freshness comes from mark-on-enter).
  useEffect(() => {
    setChannels([])
    setActiveChannelId(null)
    setOpenTopic(null)
    setFocusTopicId(null)
    if (!currentSpaceId || !showCollab) return
    reloadChannels()
    const timer = window.setInterval(reloadChannels, 30_000)
    return () => window.clearInterval(timer)
  }, [currentSpaceId, showCollab, reloadChannels])

  const selectChannel = useCallback((id: string) => {
    setActiveChannelId(id)
    setOpenTopic(null)
    setView('channel')
    // Read-on-enter: clear the badge locally first; ChannelPage pushes the watermark.
    setChannels((list) =>
      list.map((c) => (c.id === id ? { ...c, unread: 0 } : c)),
    )
  }, [])

  const createChannel = useCallback(
    async (name: string) => {
      if (!currentSpaceId) return
      try {
        const r = await channelsApi.create(currentSpaceId, { name })
        setChannels((list) => [...list, r.channel])
        selectChannel(r.channel.id)
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to create channel')
      }
    },
    [currentSpaceId, selectChannel, toast],
  )

  /** Open the topic panel; the channel's session_id may be a stale snapshot (the first
   * topic was just created), so refresh first. */
  const openTopicPanel = useCallback(
    (topic: ChannelTopic) => {
      setOpenTopic(topic)
      if (activeChannel && !activeChannel.session_id) reloadChannels()
    },
    [activeChannel, reloadChannels],
  )

  /** Board backlink → channel + auto-open the topic (ChannelPage consumes focusTopicId after replay). */
  const jumpToTopic = useCallback(
    (channelId: string, topicId: string) => {
      selectChannel(channelId)
      setFocusTopicId(topicId)
    },
    [selectChannel],
  )

  /** Board / card backlink → session view (the list may lack the new session; refetch once). */
  const jumpToSession = useCallback(
    (sessionId: string) => {
      setView('chat')
      setActiveId(sessionId)
      if (currentSpaceId) {
        sessionsApi
          .list(currentSpaceId)
          .then((r) => setSessions(r.sessions))
          .catch(() => {})
      }
    },
    [currentSpaceId],
  )

  // ---- Workflow sessions (ADR-0012): node tabs + per-tab conversation stream ----
  const [workflowInfo, setWorkflowInfo] = useState<WorkflowView | null>(null)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [advancing, setAdvancing] = useState(false)
  const [advancePreview, setAdvancePreview] = useState<AdvancePreview | null>(null)

  // ---- Template start (the template picker in the hero empty state) ----
  const [spaceTemplates, setSpaceTemplates] = useState<Template[]>([])
  const [spaceWorkflows, setSpaceWorkflows] = useState<WorkflowTemplate[]>([])
  const [startPick, setStartPick] = useState<
    | { kind: 'template'; item: Template }
    | { kind: 'workflow'; item: WorkflowTemplate }
    | null
  >(null)
  // Hero "create template" entry: jump to the templates page and open the new-template
  // editor directly (reset after consumption).
  const [templatesAutoNew, setTemplatesAutoNew] = useState(false)
  // First message of a template-started session (for optimistic rendering): during the
  // sandbox cold start (seconds to minutes) the backend emits no user_message and the
  // replay is empty; without this fallback the UI would sit in the hero empty state.
  const pendingFirstMsg = useRef<{ sid: string; content: string } | null>(null)

  const chat = useChat(activeId, workflowInfo ? activeTaskId : undefined)
  const { optimisticSend } = chat
  const active = sessions.find((s) => s.id === activeId) ?? null

  // Template start: after switching to the new session, append an optimistic user
  // message (runs after useChat's reset — declaration order guarantees it); the
  // backend's user_message event dedups by content when it arrives.
  useEffect(() => {
    const p = pendingFirstMsg.current
    if (p && p.sid === activeId) {
      pendingFirstMsg.current = null
      optimisticSend(p.content)
    }
  }, [activeId, optimisticSend])

  // Session switch: reset the workflow view; workflow sessions fetch the detail (the
  // list endpoint only carries the is_workflow flag).
  useEffect(() => {
    setWorkflowInfo(null)
    setActiveTaskId(null)
    setAdvancePreview(null)
    setAdvancing(false)
    if (!activeId) return
    let stale = false
    sessionsApi
      .get(activeId)
      .then((r) => {
        if (stale || !r.session.workflow) return
        setWorkflowInfo(r.session.workflow)
        const nodes = r.session.workflow.nodes
        const current =
          nodes.findLast?.((n) => n.task_id) ?? [...nodes].reverse().find((n) => n.task_id)
        setActiveTaskId(current?.task_id ?? null)
      })
      .catch(() => {
        /* A detail failure never blocks ordinary session use. */
      })
    return () => {
      stale = true
    }
  }, [activeId])

  // workflow_update frames (visible to every tab over SSE) → overwrite the tab-bar view.
  useEffect(() => {
    if (chat.workflow) setWorkflowInfo(chat.workflow)
  }, [chat.workflow])

  // Workspace file-path list: file-looking text in the conversation body renders as
  // chips (click opens the side-panel preview). Refreshes when a turn ends, in step
  // with the files panel; failing to fetch just means no chips — silent degradation.
  const [workspaceFiles, setWorkspaceFiles] = useState<string[]>([])
  useEffect(() => {
    if (!activeId) {
      setWorkspaceFiles([])
      return
    }
    let stale = false
    sessionsApi
      .files(activeId)
      .then((r) => {
        if (!stale) setWorkspaceFiles(r.files.map((f) => f.path))
      })
      .catch(() => {})
    return () => {
      stale = true
    }
  }, [activeId, chat.turnEndCounter])

  // Auto-select once the first node's task is in place (tasks start asynchronously on
  // session creation; tabs initially lack a task_id).
  useEffect(() => {
    if (!workflowInfo || activeTaskId) return
    const current = [...workflowInfo.nodes].reverse().find((n) => n.task_id)
    if (current?.task_id) setActiveTaskId(current.task_id)
  }, [workflowInfo, activeTaskId])

  // Template / workflow lists: refresh with the space, and refetch on returning to the
  // chat view (keeps the list fresh after edits on the templates page).
  useEffect(() => {
    if (!currentSpaceId || view !== 'chat') return
    templatesApi
      .list(currentSpaceId)
      .then((r) => setSpaceTemplates(r.templates))
      .catch(() => setSpaceTemplates([]))
    templatesApi
      .listWorkflows(currentSpaceId)
      .then((r) => setSpaceWorkflows(r.workflows))
      .catch(() => setSpaceWorkflows([]))
  }, [currentSpaceId, view])

  // Async LLM-generated session title (task D): on a session_meta frame update the
  // current session's title in place (the sidebar shares the sessions prop, so it
  // follows) without refetching the list. metaTitle only appears on the currently
  // subscribed activeId session, so update that row directly.
  useEffect(() => {
    if (!chat.metaTitle || !activeId) return
    setSessions((list) =>
      list.map((s) =>
        s.id === activeId ? { ...s, title: chat.metaTitle as string } : s,
      ),
    )
  }, [chat.metaTitle, activeId])

  // Model list: fetched once.
  useEffect(() => {
    modelsApi
      .list()
      .then((r) => setModels(r.models))
      .catch(() => {
        /* A model-list failure never blocks use; the selector degrades to "default model". */
      })
  }, [])

  // Session list: refreshes with the current space; switching spaces clears the selection.
  useEffect(() => {
    if (!currentSpaceId) return
    setSessionsLoading(true)
    setActiveId(null)
    sessionsApi
      .list(currentSpaceId)
      .then((r) => {
        setSessions(r.sessions)
        if (r.sessions.length > 0) setActiveId(r.sessions[0].id)
      })
      .catch((e) => toast(e instanceof Error ? e.message : 'Failed to load sessions'))
      .finally(() => setSessionsLoading(false))
  }, [currentSpaceId, toast])

  const defaultModel = useMemo(
    () => models.find((m) => m.default)?.id ?? models[0]?.id ?? '',
    [models],
  )
  // When the draft model is empty / missing from the list, restore from the persisted
  // preference (falling back to the default model), so a refresh keeps the user's last
  // model and effort choice. While models have not loaded (empty list),
  // resolveModelPref returns null → skip and wait for the next round, avoiding wiping
  // a valid preference back to the default.
  useEffect(() => {
    if (!draftModel || !models.some((m) => m.id === draftModel)) {
      const resolved = resolveModelPref(models, defaultModel)
      if (!resolved) return
      setDraftModel(resolved.model)
      setDraftEffort(resolved.effort)
    }
  }, [defaultModel, draftModel, models])
  const currentModel = activeId
    ? (modelBySession[activeId] ?? active?.model ?? defaultModel)
    : draftModel
  const currentModelDef = models.find((m) => m.id === currentModel)
  // effort: session-level choice → current model's default_effort → empty.
  const currentEffort = activeId
    ? ((effortBySession[activeId] ?? currentModelDef?.default_effort) ?? '')
    : (draftEffort || currentModelDef?.default_effort || '')

  const onModelChange = useCallback(
    (id: string) => {
      const newDef = models.find((m) => m.id === id)
      if (!activeId) {
        setDraftModel(id)
        // Switching models: reset the effort to the new model's default when the
        // current one is not in its efforts list.
        let eff = draftEffort
        if (newDef?.efforts && draftEffort && !newDef.efforts.includes(draftEffort)) {
          eff = newDef.default_effort ?? ''
          setDraftEffort(eff)
        }
        writeModelPref({ model: id, effort: eff }) // Remember for the next new session.
        return
      }
      setModelBySession((m) => ({ ...m, [activeId]: id }))
      // Switching models: reset the effort to the new model's default when the current
      // one is not in its efforts list.
      const prev = effortBySession[activeId] ?? currentModelDef?.default_effort
      let eff = prev ?? ''
      if (newDef?.efforts && prev && !newDef.efforts.includes(prev)) {
        eff = newDef.default_effort ?? ''
        setEffortBySession((e) => ({ ...e, [activeId]: eff }))
      }
      writeModelPref({ model: id, effort: eff })
    },
    [activeId, models, effortBySession, currentModelDef, draftEffort],
  )

  const onEffortChange = useCallback(
    (effort: string) => {
      writeModelPref({ model: currentModel, effort })
      if (!activeId) {
        setDraftEffort(effort)
        return
      }
      setEffortBySession((e) => ({ ...e, [activeId]: effort }))
    },
    [activeId, currentModel],
  )

  // "New session" = open a blank ready-to-send page (hero empty state) with nothing
  // persisted; the real session is created implicitly by sendMessage on the first
  // message (reusing the existing activeId===null path), so each click does not
  // conjure an empty session. The draft model/effort carries over the last choice
  // (default when none).
  const newSession = useCallback(() => {
    setActiveId(null)
    setAdminTraceSessionId(null)
    const resolved = resolveModelPref(models, defaultModel)
    setDraftModel(resolved?.model ?? defaultModel)
    setDraftEffort(resolved?.effort ?? '')
    if (view !== 'chat') setView('chat')
    if (window.innerWidth < 1024) setSidebarOpen(false)
  }, [defaultModel, models, view])

  const changeView = useCallback((next: MainView) => {
    if (next === 'admin') setAdminTraceSessionId(null)
    setView(next)
  }, [])

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await sessionsApi.remove(id)
        setSessions((list) => {
          const next = list.filter((s) => s.id !== id)
          setActiveId((cur) => (cur === id ? (next[0]?.id ?? null) : cur))
          return next
        })
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to delete')
      }
    },
    [toast],
  )

  const sendMessage = useCallback(
    async (content: string, images: ImageAttachment[] = []) => {
      let sid = activeId
      try {
        // No session yet: create one implicitly first (owned by the current space).
        if (!sid) {
          if (!currentSpaceId) throw new Error('No space selected')
          const r = await sessionsApi.create(currentSpaceId, currentModel || undefined)
          setSessions((list) => [r.session, ...list])
          setKnownEmpty((set) => new Set(set).add(r.session.id))
          // Land the draft model/effort on the new session (so the send is not
          // overridden by the backend default, and switching away and back keeps the
          // user's choice).
          setModelBySession((m) => ({
            ...m,
            [r.session.id]: currentModel || r.session.model,
          }))
          if (currentEffort) {
            setEffortBySession((e) => ({ ...e, [r.session.id]: currentEffort }))
          }
          setActiveId(r.session.id)
          sid = r.session.id
        }
        await sessionsApi.sendMessage(
          sid,
          content,
          currentModel || undefined,
          currentEffort || undefined,
          workflowInfo ? (activeTaskId ?? undefined) : undefined,
          images,
        )
        // Optimistic rendering: while seed_start blocks on the sandbox cold start,
        // user_message only emits once the container is ready — push the message into
        // items + running=true here and let foldFrame dedup when the real event
        // arrives. For a continued session (sid already the activeId) this applies
        // directly; for a just-created session (activeId just set) chat still points
        // at the old null session and the dispatch is lost — that case is covered by
        // the backend's synthetic turn_started push. Attached images preview from
        // their local data URLs until the real event brings the content hashes.
        optimisticSend(content, images.map(dataUrlFromAttachment))
        // The title is generated by the backend from the first message; update locally first.
        setSessions((list) =>
          list.map((s) =>
            s.id === sid && (!s.title || s.title === 'New session')
              ? { ...s, title: content.slice(0, 40) }
              : s,
          ),
        )
        // Close the sidebar after sending (narrow screens): go straight to the conversation.
        if (window.innerWidth < 1024) setSidebarOpen(false)
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          toast('The agent is still working on the previous turn — stop it or wait for it to finish', 'info')
        } else {
          toast(e instanceof Error ? e.message : 'Failed to send')
        }
        throw e
      }
    },
    [activeId, currentModel, currentEffort, currentSpaceId, optimisticSend,
     toast, workflowInfo, activeTaskId],
  )

  const stop = useCallback(() => {
    if (!activeId) return
    sessionsApi
      .cancel(activeId, workflowInfo ? (activeTaskId ?? undefined) : undefined)
      .catch((e) => {
        toast(e instanceof Error ? e.message : 'Failed to stop')
      })
  }, [activeId, toast, workflowInfo, activeTaskId])

  const copyTraceId = useCallback(async () => {
    if (!activeId) return
    try {
      await copyText(activeId)
      toast('Trace ID copied', 'info')
    } catch {
      toast('Copy failed — copy the Trace ID manually')
    }
  }, [activeId, toast])

  const openTrace = useCallback(() => {
    if (!activeId) return
    setAdminTraceSessionId(activeId)
    setView('admin')
  }, [activeId])

  const answer = useCallback(
    async (questionId: string, answers: AnswerPayload) => {
      if (!activeId) return
      await sessionsApi.answer(
        activeId, questionId, answers,
        workflowInfo ? (activeTaskId ?? undefined) : undefined,
      )
      chat.markAnswered(questionId)
    },
    [activeId, chat, workflowInfo, activeTaskId],
  )

  // ---- Template / workflow start ----
  const startFromTemplate = useCallback(
    async (
      pick: { kind: 'template'; item: Template } | { kind: 'workflow'; item: WorkflowTemplate },
      params: Record<string, string>,
    ) => {
      if (!currentSpaceId) return
      try {
        const r = await sessionsApi.create(currentSpaceId, currentModel || undefined, {
          ...(pick.kind === 'template'
            ? { templateId: pick.item.id }
            : { workflowTemplateId: pick.item.id }),
          params,
        })
        // Single-template start = the substituted prompt becomes the first message;
        // render it optimistically (workflow sessions skip the hero — the workflow
        // view presents its own starting state).
        if (pick.kind === 'template') {
          pendingFirstMsg.current = {
            sid: r.session.id,
            content: renderPrompt(pick.item.prompt, params),
          }
        }
        setSessions((list) => [r.session, ...list])
        setActiveId(r.session.id)
        setStartPick(null)
        if (window.innerWidth < 1024) setSidebarOpen(false)
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to start')
        throw e
      }
    },
    [currentSpaceId, currentModel, toast],
  )

  /** Template card click: open the form when params are needed, otherwise start directly. */
  const pickTemplate = useCallback(
    (pick: { kind: 'template'; item: Template } | { kind: 'workflow'; item: WorkflowTemplate }) => {
      const params =
        pick.kind === 'template'
          ? pick.item.params
          : (spaceTemplates.find(
              (t) => t.id === pick.item.nodes[0]?.template_id,
            )?.params ?? [])
      if (params.length === 0) {
        void startFromTemplate(pick, {})
      } else {
        setStartPick(pick)
      }
    },
    [spaceTemplates, startFromTemplate],
  )

  /** First-node parameter definitions (for StartTemplateModal): a workflow takes the
   * params of the template its first node references. */
  const startPickParams = useMemo(() => {
    if (!startPick) return []
    if (startPick.kind === 'template') return startPick.item.params
    return (
      spaceTemplates.find((t) => t.id === startPick.item.nodes[0]?.template_id)
        ?.params ?? []
    )
  }, [startPick, spaceTemplates])

  /** Display prompt (left column of StartTemplateModal): a workflow takes the first node's template prompt. */
  const startPickPrompt = useMemo(() => {
    if (!startPick) return ''
    if (startPick.kind === 'template') return startPick.item.prompt
    return (
      spaceTemplates.find((t) => t.id === startPick.item.nodes[0]?.template_id)
        ?.prompt ?? ''
    )
  }, [startPick, spaceTemplates])

  // ---- Workflow advance (two-phase, ADR-0012 D11) ----
  const onAdvance = useCallback(async () => {
    if (!activeId || advancing) return
    setAdvancing(true)
    try {
      const preview = await sessionsApi.advancePreview(activeId)
      setAdvancePreview(preview)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to generate handoff content')
    } finally {
      setAdvancing(false)
    }
  }, [activeId, advancing, toast])

  const onAdvanceConfirm = useCallback(
    async (params: Record<string, string>, summary: string) => {
      if (!activeId || !advancePreview) return
      const nodeIndex = advancePreview.node_index
      try {
        await sessionsApi.advanceConfirm(activeId, {
          node_index: nodeIndex,
          params,
          summary,
        })
        setAdvancePreview(null)
        // Poll the session detail until the new node's task is in place, then switch (usually <1s).
        for (let i = 0; i < 20; i++) {
          const r = await sessionsApi.get(activeId)
          const node = r.session.workflow?.nodes[nodeIndex]
          if (r.session.workflow) setWorkflowInfo(r.session.workflow)
          if (node?.task_id) {
            setActiveTaskId(node.task_id)
            return
          }
          await new Promise((resolve) => setTimeout(resolve, 300))
        }
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Failed to advance to the next stage')
      }
    },
    [activeId, advancePreview, toast],
  )

  const question = pendingQuestion(chat.items)
  // Latest checklist snapshot (todo_update replaces wholesale; scan items back to
  // front): lives in the TodoStrip above the composer; the main flow renders no card
  // (Conversation skips todos items).
  const todos = useMemo(() => {
    for (let i = chat.items.length - 1; i >= 0; i--) {
      const it = chat.items[i]
      if (it.kind === 'todos') return it.todos
    }
    return null
  }, [chat.items])
  const isEmpty =
    chat.items.length === 0 &&
    (chat.connected || (activeId !== null && knownEmpty.has(activeId)))
  // Workflow sessions skip the hero (items are briefly empty before the node task
  // starts); running empty sessions skip it too (during the sandbox cold-start window
  // after a template start + page refresh the replay is empty — falling back to the
  // hero would falsely suggest nothing was sent).
  const showHero =
    view === 'chat' &&
    !workflowInfo &&
    (activeId === null || (isEmpty && active?.status !== 'running'))

  return (
    <div className="flex h-full">
      {/* Session sidebar: static on desktop, a drawer on narrow screens. */}
      <div
        className={cn(
          'z-40 shrink-0 transition-[margin] duration-200 max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:shadow-xl',
          sidebarOpen ? '' : 'max-lg:hidden lg:-ml-64',
        )}
      >
        <Sidebar
          sessions={sessions}
          loading={sessionsLoading}
          activeId={activeId}
          view={view}
          running={chat.running}
          showCollab={showCollab}
          channels={channels}
          activeChannelId={activeChannelId}
          onSelectChannel={selectChannel}
          onOpenBoard={() => setView('board')}
          onCreateChannel={createChannel}
          onViewChange={changeView}
          onSelect={(id) => {
            setActiveId(id)
            setAdminTraceSessionId(null)
            // Selecting a session always returns to chat (from trace/skills/knowledge etc.).
            if (view !== 'chat') setView('chat')
            if (window.innerWidth < 1024) setSidebarOpen(false)
          }}
          onCreate={newSession}
          onDelete={(id) => void deleteSession(id)}
          onToggle={() => setSidebarOpen(false)}
        />
      </div>
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-30 bg-black/30 lg:hidden"
        />
      )}
      {/* Main column */}
      <main className="flex min-w-0 flex-1 flex-col">
        {/* Top bar: lines up with the 52px top bars of both side rails. */}
        <div className="flex h-[52px] shrink-0 items-center gap-1.5 border-b border-border px-3">
          {!sidebarOpen && (
            <button
              type="button"
              title="Open session sidebar"
              onClick={() => setSidebarOpen(true)}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
            >
              <IconSidebar />
            </button>
          )}
          {view === 'channel' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              <span className="mr-1 font-mono text-ink-3">#</span>
              {activeChannel?.name ?? 'Channel'}
            </span>
          ) : view === 'board' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Task board
            </span>
          ) : view === 'skills' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Skills
            </span>
          ) : view === 'templates' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Templates
            </span>
          ) : view === 'knowledge' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Knowledge base
            </span>
          ) : view === 'memories' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Memories
            </span>
          ) : view === 'feedback' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Feedback
            </span>
          ) : view === 'settings' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Settings
            </span>
          ) : view === 'admin' ? (
            <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
              Admin console
            </span>
          ) : (
            <>
              <span className="min-w-0 truncate text-[15px] font-semibold text-ink">
                {active?.title || 'New session'}
              </span>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                {activeId && (
                  <button
                    type="button"
                    onClick={() => void copyTraceId()}
                    title="Copy Trace ID"
                    className="flex h-7 min-w-0 max-w-[11rem] items-center gap-1.5 rounded-md border border-border bg-bg px-2 font-mono text-[11px] text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
                  >
                    <span className="shrink-0 text-ink-3">Trace ID</span>
                    <span className="min-w-0 truncate">{activeId}</span>
                    <IconCopy className="h-3.5 w-3.5 shrink-0" />
                  </button>
                )}
                {user?.is_admin && activeId && (
                  <button
                    type="button"
                    onClick={openTrace}
                    title="View trace"
                    className="flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border bg-bg px-2 text-[11.5px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
                  >
                    <IconTrace className="h-3.5 w-3.5" />
                    Trace
                  </button>
                )}
              </div>
            </>
          )}
        </div>
        {/* sockOpen covers both disconnect shapes — a fetch error and a quietly ended
            stream; the config pages (skills / knowledge / settings) are unrelated to
            chat and show no reconnect bar. */}
        <ReconnectBanner active={view === 'chat' && activeId !== null && !chat.sockOpen} />
        {view === 'channel' ? (
          activeChannel ? (
            <ChannelPage
              key={activeChannel.id}
              channel={activeChannel}
              onOpenTopic={openTopicPanel}
              onTopicToCard={(topic: ChannelTopic) => {
                channelsApi
                  .topicToCard(activeChannel.id, topic.id)
                  .then(() => toast('Converted to a board card', 'info'))
                  .catch((e) =>
                    toast(e instanceof Error ? e.message : 'Failed to convert to a card'),
                  )
              }}
              focusTopicId={focusTopicId}
              onFocusConsumed={() => setFocusTopicId(null)}
              currentUser={user?.username}
            />
          ) : (
            <div className="flex flex-1 items-center justify-center text-[13px] text-ink-3">
              This channel does not exist or is archived
            </div>
          )
        ) : view === 'board' ? (
          currentSpaceId ? (
            <BoardPage
              key={currentSpaceId}
              spaceId={currentSpaceId}
              currentUser={user?.username}
              isSpaceOwner={isSpaceOwner}
              onOpenTopic={jumpToTopic}
              onOpenSession={jumpToSession}
            />
          ) : null
        ) : view === 'skills' ? (
          <SkillsPage />
        ) : view === 'templates' ? (
          <TemplatesPage
            autoNew={templatesAutoNew}
            onAutoNewDone={() => setTemplatesAutoNew(false)}
          />
        ) : view === 'knowledge' ? (
          <KnowledgePage />
        ) : view === 'memories' ? (
          <MemoriesPage />
        ) : view === 'feedback' ? (
          <FeedbackPage
            onOpenSession={(sid: string) => {
              setActiveId(sid)
              setView('chat')
            }}
          />
        ) : view === 'settings' ? (
          <UserSettingsPage />
        ) : view === 'admin' ? (
          <AdminPage initialTraceSessionId={adminTraceSessionId} />
        ) : showHero ? (
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-4 pb-16">
            <div className="msg-enter w-full max-w-2xl">
              <div className="mb-3 flex justify-center">
                <Logo size="lg" running={chat.running} />
              </div>
              <p className="mb-10 text-center text-[14px] text-ink-3">
                Noeta Agent — describe the task, paste your docs, and leave the rest to it.
              </p>
              <Composer
                hero
                onSend={sendMessage}
                onStop={stop}
                running={chat.running}
                disabled={false}
                models={models}
                model={currentModel}
                onModelChange={onModelChange}
                effort={currentEffort}
                onEffortChange={onEffortChange}
              />
              {/* Template / workflow start area (ADR-0012): card grid, pick → fill
                  params → create the session; owners see it even when empty (exposing
                  the "create template" card). */}
              {(spaceTemplates.length > 0 ||
                spaceWorkflows.length > 0 ||
                isSpaceOwner) && (
                <div className="mt-8">
                  <p className="mb-3 text-center font-mono text-[11px] uppercase tracking-[0.12em] text-ink-3">
                    Or start from a template
                  </p>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {isSpaceOwner && (
                      <button
                        type="button"
                        onClick={() => {
                          setTemplatesAutoNew(true)
                          changeView('templates')
                        }}
                        className="group flex min-h-[104px] flex-col items-start justify-between rounded-xl border border-dashed border-border px-4 py-3 text-left transition-colors hover:border-border-strong"
                      >
                        <div>
                          <p className="text-[13.5px] font-medium text-ink">
                            Create a template
                          </p>
                          <p className="mt-1 text-[12px] text-ink-3">
                            Capture reusable instructions and know-how
                          </p>
                        </div>
                        <span className="flex items-center gap-1 text-[12px] text-ink-2 group-hover:text-ink">
                          <IconPlus className="h-3 w-3" />
                          New
                        </span>
                      </button>
                    )}
                    {spaceWorkflows.map((w) => (
                      <button
                        key={w.id}
                        type="button"
                        onClick={() => pickTemplate({ kind: 'workflow', item: w })}
                        className="group flex min-h-[104px] flex-col rounded-xl border border-border bg-surface px-4 py-3 text-left transition-colors hover:border-border-strong"
                      >
                        <div className="flex w-full items-center gap-2">
                          <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">
                            {w.name}
                          </span>
                          <span className="shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-accent">
                            Workflow · {w.nodes.length} {w.nodes.length === 1 ? 'node' : 'nodes'}
                          </span>
                        </div>
                        <p className="mt-1 line-clamp-2 w-full flex-1 text-[12px] text-ink-3">
                          {w.description}
                        </p>
                        <div className="mt-2 flex w-full items-center justify-between gap-2">
                          <span className="min-w-0 truncate font-mono text-[11px] text-ink-3">
                            {w.nodes
                              .map((n) => n.template_name ?? '?')
                              .join(' → ')}
                          </span>
                          <span className="shrink-0 text-[13px] text-ink-3 transition-transform group-hover:translate-x-0.5 group-hover:text-ink">
                            →
                          </span>
                        </div>
                      </button>
                    ))}
                    {spaceTemplates.map((tp) => (
                      <button
                        key={tp.id}
                        type="button"
                        onClick={() => pickTemplate({ kind: 'template', item: tp })}
                        className="group flex min-h-[104px] flex-col rounded-xl border border-border bg-surface px-4 py-3 text-left transition-colors hover:border-border-strong"
                      >
                        <div className="flex w-full items-center gap-2">
                          <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">
                            {tp.name}
                          </span>
                          <span className="shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-ink-3">
                            Template
                          </span>
                        </div>
                        <p className="mt-1 line-clamp-2 w-full flex-1 text-[12px] text-ink-3">
                          {tp.description}
                        </p>
                        <div className="mt-2 flex w-full items-center justify-between gap-2">
                          <span className="font-mono text-[11px] text-ink-3">
                            {tp.params.length > 0
                              ? `${tp.params.length} param${tp.params.length === 1 ? '' : 's'}`
                              : 'No params'}
                          </span>
                          <span className="shrink-0 text-[13px] text-ink-3 transition-transform group-hover:translate-x-0.5 group-hover:text-ink">
                            →
                          </span>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        ) : (
          <>
            {workflowInfo && (
              <TaskTabs
                workflow={workflowInfo}
                activeTaskId={activeTaskId}
                onSelect={setActiveTaskId}
                onAdvance={() => void onAdvance()}
                advancing={advancing}
              />
            )}
            <Conversation
              // Remount per session + node tab: isolates each stream's fold state.
              key={`${activeId ?? 'draft'}:${activeTaskId ?? ''}`}
              items={chat.items}
              running={chat.running}
              connected={chat.connected}
              connectionError={chat.connectionError}
              spaceId={currentSpaceId}
              sessionId={activeId}
              taskId={workflowInfo ? activeTaskId : undefined}
              userAvatar={user?.avatar}
              userName={user?.name || user?.username}
              streamingBlocks={renderBlocks(chat.streaming)}
              onAnswer={answer}
              onOpenDoc={panel.openDoc}
              workspaceFiles={workspaceFiles}
              onOpenFile={panel.openWorkspaceFile}
            />
            <div className="shrink-0 px-4 pb-4 sm:px-6">
              <div className="mx-auto max-w-3xl">
                {/* Persistent checklist strip: renders only when todos exist; updates live with todo_update. */}
                {todos && todos.length > 0 && <TodoStrip todos={todos} />}
                {question && !chat.running && (
                  <p className="mb-2 text-center font-mono text-[11px] text-accent">
                    · The agent is waiting for your answer above ·
                  </p>
                )}
                <Composer
                  onSend={sendMessage}
                  onStop={stop}
                  running={chat.running}
                  disabled={false}
                  models={models}
                  model={currentModel}
                  onModelChange={onModelChange}
                  effort={currentEffort}
                  onEffortChange={onEffortChange}
                />
              </div>
            </div>
          </>
        )}
      </main>

      {/* Right multi-tab panel (available in the session view). */}
      {view === 'chat' && activeId && (
        <SidePanel
          sessionId={activeId}
          refreshKey={chat.turnEndCounter}
          panel={panel}
        />
      )}

      {/* Topic panel (opened from a topic card in the channel view; replay only works
          after session_id is lazily backfilled). */}
      {view === 'channel' && activeChannel?.session_id && openTopic && (
        <TopicPanel
          key={openTopic.id}
          channel={activeChannel}
          topic={openTopic}
          spaceId={currentSpaceId}
          userAvatar={user?.avatar}
          userName={user?.name || user?.username}
          onClose={() => setOpenTopic(null)}
        />
      )}

      {/* Template-start parameter form */}
      {startPick && (
        <StartTemplateModal
          title={startPick.item.name}
          description={startPick.item.description}
          prompt={startPickPrompt}
          nodeNames={
            startPick.kind === 'workflow'
              ? startPick.item.nodes.map((n) => n.template_name ?? '?')
              : undefined
          }
          params={startPickParams}
          onSubmit={(values: Record<string, string>) => startFromTemplate(startPick, values)}
          onClose={() => setStartPick(null)}
        />
      )}
      {/* Workflow advance confirmation (handoff prefill + handoff summary) */}
      {advancePreview && (
        <AdvanceDialog
          preview={advancePreview}
          onConfirm={onAdvanceConfirm}
          onClose={() => setAdvancePreview(null)}
        />
      )}
    </div>
  )
}
