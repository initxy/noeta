import { api } from './client'
import type {
  AdminConfigItem,
  AdvancePreview,
  AdminSession,
  AdminSpace,
  AdminStats,
  AdminUser,
  AgentConfig,
  AnswerPayload,
  AuthConfig,
  BoardCard,
  Channel,
  ChannelMessage,
  ChannelTopic,
  FeedbackCounts,
  FeedbackEntry,
  FeedbackReport,
  FeedbackRun,
  FeedbackSuggestion,
  FileContent,
  FileEntry,
  KnowledgeSource,
  McpConnector,
  McpPromptInfo,
  McpResourceInfo,
  McpToolInfo,
  MemoryEntry,
  ModelInfo,
  Paginated,
  PreviewContent,
  PreviewEntry,
  RawEnvelope,
  ResolvedKnowledgePath,
  SandboxPreview,
  Session,
  Skill,
  Space,
  Template,
  WorkflowTemplate,
  SpaceDetail,
  SpaceMember,
  SyncProgress,
  User,
} from './types'

const BASE = '/api/v1'

export const authApi = {
  getConfig: () => api.get<AuthConfig>(`${BASE}/auth/config`),
  me: () => api.get<{ user: User }>(`${BASE}/auth/me`),
  devLogin: (username: string) =>
    api.post<{ user: User }>(`${BASE}/auth/dev-login`, { username }),
  logout: () => api.post<{ ok: boolean }>(`${BASE}/auth/logout`),
}

export const modelsApi = {
  list: () => api.get<{ models: ModelInfo[] }>(`${BASE}/models`),
}

/** Space CRUD + member management. Response shapes mirror the backend (mostly wrapped under space/spaces/members). */
export const spacesApi = {
  list: () => api.get<{ spaces: Space[] }>(`${BASE}/spaces`),
  create: (data: { name: string; description?: string }) =>
    api.post<{ space: Space }>(`${BASE}/spaces`, data),
  get: (id: string) => api.get<{ space: SpaceDetail }>(`${BASE}/spaces/${id}`),
  update: (id: string, data: { name?: string; description?: string }) =>
    api.patch<{ space: Space }>(`${BASE}/spaces/${id}`, data),
  remove: (id: string) => api.delete<{ ok: boolean }>(`${BASE}/spaces/${id}`),
  addMember: (
    spaceId: string,
    data: { username?: string; email?: string; role?: 'owner' | 'member' },
  ) => api.post<{ members: SpaceMember[] }>(`${BASE}/spaces/${spaceId}/members`, data),
  updateMemberRole: (spaceId: string, username: string, role: 'owner' | 'member') =>
    api.patch<{ members: SpaceMember[] }>(
      `${BASE}/spaces/${spaceId}/members/${encodeURIComponent(username)}`,
      { role },
    ),
  removeMember: (spaceId: string, username: string) =>
    api.delete<{ members: SpaceMember[] }>(
      `${BASE}/spaces/${spaceId}/members/${encodeURIComponent(username)}`,
    ),
}

export const usersApi = {
  search: (q: string, limit = 10) =>
    api.get<{ users: User[] }>(
      `${BASE}/users/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
}

/** Space-level agent configuration (prompt / capability toggles / knowledge-source selection / default model). */
export const agentConfigApi = {
  get: (spaceId: string) =>
    api.get<{ config: AgentConfig }>(`${BASE}/spaces/${spaceId}/agent-config`),
  update: (
    spaceId: string,
    data: Partial<Omit<AgentConfig, 'knowledge_sources'>> & {
      knowledge_sources?: string[]
      clear_knowledge_sources?: boolean
    },
  ) => api.put<{ config: AgentConfig }>(`${BASE}/spaces/${spaceId}/agent-config`, data),
}

/** Admin builtin-skill management (platform scope; everything lives in the shared builtin-skills/ directory, ADR-0010). */
export const skillsApi = {
  list: () => api.get<{ skills: Skill[] }>(`${BASE}/skills`),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<{ skill: Skill }>(`${BASE}/skills`, form)
  },
  /** Enable / disable globally (platform-wide; reflected when new sessions are assembled). */
  setEnabled: (name: string, enabled: boolean) =>
    api.patch<{ ok: boolean; name: string; enabled: boolean }>(
      `${BASE}/skills/${encodeURIComponent(name)}`,
      { enabled },
    ),
  /** Delete a builtin skill (removes the directory + table row for real). */
  remove: (name: string) =>
    api.delete<{ ok: boolean }>(`${BASE}/skills/${encodeURIComponent(name)}`),
  /** Builtin content preview (read-only): no path returns the file tree; with a path returns file content. */
  preview: (
    name: string,
    path?: string,
  ): Promise<{ entries: PreviewEntry[] } | PreviewContent> =>
    api.get<{ entries: PreviewEntry[] } | PreviewContent>(
      `${BASE}/skills/${encodeURIComponent(name)}/preview${
        path ? `?path=${encodeURIComponent(path)}` : ''
      }`,
    ),
}

/** Space-scoped skill list (builtin + skills uploaded to this space). */
export const spaceSkillsApi = {
  list: (spaceId: string) =>
    api.get<{ skills: Skill[] }>(`${BASE}/spaces/${spaceId}/skills`),
  upload: (spaceId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post<{ skill: Skill }>(`${BASE}/spaces/${spaceId}/skills`, form)
  },
  remove: (spaceId: string, name: string) =>
    api.delete<{ ok: boolean }>(
      `${BASE}/spaces/${spaceId}/skills/${encodeURIComponent(name)}`,
    ),
  /** Enable / disable a skill (owner only): applies uniformly to builtin and space skills; takes effect for new sessions. */
  setEnabled: (spaceId: string, name: string, enabled: boolean) =>
    api.patch<{ ok: boolean; name: string; enabled: boolean }>(
      `${BASE}/spaces/${spaceId}/skills/${encodeURIComponent(name)}`,
      { enabled },
    ),
  /** Assign / clear a group (owner only): group=null clears. Builtin skills cannot be grouped.
   * Bulk grouping is done by the frontend calling this single-item endpoint concurrently per name. */
  setGroup: (spaceId: string, name: string, group: string | null) =>
    api.put<{ ok: boolean; name: string; group: string | null }>(
      `${BASE}/spaces/${spaceId}/skills/${encodeURIComponent(name)}/group`,
      { group },
    ),
  /** Skill preview (read-only): no path returns the file tree; with a path returns file content. Builtins return 403. */
  preview: (
    spaceId: string,
    name: string,
    path?: string,
  ): Promise<{ entries: PreviewEntry[] } | PreviewContent> =>
    api.get<{ entries: PreviewEntry[] } | PreviewContent>(
      `${BASE}/spaces/${spaceId}/skills/${encodeURIComponent(name)}/preview${
        path ? `?path=${encodeURIComponent(path)}` : ''
      }`,
    ),
}

/** Space knowledge base: knowledge-source CRUD + sync trigger/status. */
export const knowledgeApi = {
  list: (spaceId: string) =>
    api.get<{ sources: KnowledgeSource[] }>(`${BASE}/spaces/${spaceId}/knowledge`),
  create: (
    spaceId: string,
    data: { name: string; type: string; config: Record<string, unknown> },
  ) =>
    api.post<{ source: KnowledgeSource }>(
      `${BASE}/spaces/${spaceId}/knowledge`,
      data,
    ),
  update: (
    spaceId: string,
    sourceId: string,
    data: { name?: string; config?: Record<string, unknown> },
  ) =>
    api.patch<{ source: KnowledgeSource }>(
      `${BASE}/spaces/${spaceId}/knowledge/${sourceId}`,
      data,
    ),
  remove: (spaceId: string, sourceId: string) =>
    api.delete<{ ok: boolean }>(
      `${BASE}/spaces/${spaceId}/knowledge/${sourceId}`,
    ),
  sync: (spaceId: string, sourceId: string) =>
    api.post<{ source: KnowledgeSource }>(
      `${BASE}/spaces/${spaceId}/knowledge/${sourceId}/sync`,
      {},
    ),
  syncStatus: (spaceId: string, sourceId: string) =>
    api.get<{
      status: string
      last_sync_at: number | null
      last_error: string | null
      progress: SyncProgress | null
      report: null
    }>(`${BASE}/spaces/${spaceId}/knowledge/${sourceId}/sync`),
  /** Batch-resolve citation paths (readable by members): knowledge/<source name>/<path>[#anchor]
   * → title / origin link / excerpt. At most 50 per call (callers batch). */
  resolvePaths: (spaceId: string, paths: string[]) =>
    api.post<{ items: ResolvedKnowledgePath[] }>(
      `${BASE}/spaces/${spaceId}/knowledge/resolve-paths`,
      { paths },
    ),
}

/** Per-space MCP connectors: CRUD + enable toggle + tool subset + discovery
 * menus. Members read; owners manage. Credential values are write-only —
 * every response carries scrubbed name lists. */
export const mcpApi = {
  list: (spaceId: string) =>
    api.get<{ servers: McpConnector[] }>(`${BASE}/spaces/${spaceId}/mcp/servers`),
  /** Create or replace a connector (same alias overwrites). */
  create: (
    spaceId: string,
    data: {
      alias: string
      type: 'http' | 'stdio'
      url?: string
      headers?: Record<string, string>
      command?: string
      args?: string[]
      env?: Record<string, string>
      tools?: string[] | null
      enabled?: boolean
    },
  ) =>
    api.post<{ server: McpConnector }>(
      `${BASE}/spaces/${spaceId}/mcp/servers`,
      data,
    ),
  /** Merge edit: an omitted field keeps its stored value (credentials never
   * need re-pasting). */
  update: (
    spaceId: string,
    alias: string,
    data: {
      url?: string
      headers?: Record<string, string>
      command?: string
      args?: string[]
      env?: Record<string, string>
      tools?: string[] | null
    },
  ) =>
    api.put<{ server: McpConnector }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}`,
      data,
    ),
  setEnabled: (spaceId: string, alias: string, enabled: boolean) =>
    api.patch<{ server: McpConnector }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}`,
      { enabled },
    ),
  remove: (spaceId: string, alias: string) =>
    api.delete<{ ok: boolean }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}`,
    ),
  /** Set the enabled-tool subset (null = all advertised tools). */
  setTools: (spaceId: string, alias: string, tools: string[] | null) =>
    api.put<{ server: McpConnector }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}/tools`,
      { tools },
    ),
  /** Discovery menus (http connectors only): connect + list. */
  toolMenu: (spaceId: string, alias: string) =>
    api.get<{ tools: McpToolInfo[] }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}/tools`,
    ),
  promptMenu: (spaceId: string, alias: string) =>
    api.get<{ prompts: McpPromptInfo[] }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}/prompts`,
    ),
  resourceMenu: (spaceId: string, alias: string) =>
    api.get<{ resources: McpResourceInfo[] }>(
      `${BASE}/spaces/${spaceId}/mcp/servers/${encodeURIComponent(alias)}/resources`,
    ),
}

/** Space memory management: view / edit / archive / delete of agent long-term memory
 * (a markdown file pool). Members can read, edit, archive; physical delete is owner
 * only (backend memories.py permission model). */
export const memoriesApi = {
  list: (spaceId: string) =>
    api.get<{ memories: MemoryEntry[] }>(`${BASE}/spaces/${spaceId}/memories`),
  get: (spaceId: string, name: string) =>
    api.get<{ name: string; text: string }>(
      `${BASE}/spaces/${spaceId}/memories/${name}`,
    ),
  write: (spaceId: string, name: string, text: string) =>
    api.put<{ ok: boolean }>(`${BASE}/spaces/${spaceId}/memories/${name}`, { text }),
  archive: (spaceId: string, name: string) =>
    api.post<{ ok: boolean }>(
      `${BASE}/spaces/${spaceId}/memories/${name}/archive`,
      {},
    ),
  remove: (spaceId: string, name: string) =>
    api.delete<{ ok: boolean }>(`${BASE}/spaces/${spaceId}/memories/${name}`),
}

/** Feedback loop (ADR-0017): message-level thumbs collection, reference attachment,
 * analysis runs and the suggestion surface. Submit / view / attach reference = space
 * members; trigger analysis / adopt / dismiss = owner only. */
export const feedbackApi = {
  submit: (
    sessionId: string,
    body: {
      rating: 1 | -1
      task_id?: string
      event_seq?: number
      tags?: string[]
      comment?: string
    },
  ) =>
    api.post<{ feedback: FeedbackEntry }>(
      `${BASE}/sessions/${sessionId}/feedback`,
      body,
    ),
  listForSession: (sessionId: string) =>
    api.get<{ feedback: FeedbackEntry[] }>(
      `${BASE}/sessions/${sessionId}/feedback`,
    ),
  list: (spaceId: string) =>
    api.get<{
      feedback: FeedbackEntry[]
      counts: FeedbackCounts
      tags: string[]
    }>(`${BASE}/spaces/${spaceId}/feedback`),
  putReference: (
    spaceId: string,
    feedbackId: string,
    body: { kind: 'text'; text?: string },
  ) =>
    api.put<{ feedback: FeedbackEntry }>(
      `${BASE}/spaces/${spaceId}/feedback/${feedbackId}/reference`,
      body,
    ),
  getReference: (spaceId: string, feedbackId: string) =>
    api.get<{ feedback_id: string; text: string }>(
      `${BASE}/spaces/${spaceId}/feedback/${feedbackId}/reference`,
    ),
  analyze: (spaceId: string) =>
    api.post<{ run: FeedbackRun; feedback_count: number }>(
      `${BASE}/spaces/${spaceId}/feedback/analyze`,
    ),
  latestRun: (spaceId: string) =>
    api.get<{ run: FeedbackRun | null }>(
      `${BASE}/spaces/${spaceId}/feedback/runs/latest`,
    ),
  suggestions: (spaceId: string) =>
    api.get<{ suggestions: FeedbackSuggestion[] }>(
      `${BASE}/spaces/${spaceId}/feedback/suggestions`,
    ),
  adopt: (
    spaceId: string,
    suggestionId: string,
    body: { memory_name?: string; memory_text?: string },
  ) =>
    api.post<{ suggestion: FeedbackSuggestion }>(
      `${BASE}/spaces/${spaceId}/feedback/suggestions/${suggestionId}/adopt`,
      body,
    ),
  dismiss: (spaceId: string, suggestionId: string) =>
    api.post<{ suggestion: FeedbackSuggestion }>(
      `${BASE}/spaces/${spaceId}/feedback/suggestions/${suggestionId}/dismiss`,
    ),
  skillDiff: (spaceId: string, suggestionId: string) =>
    api.get<{ skill_name: string; current: string; patched: string }>(
      `${BASE}/spaces/${spaceId}/feedback/suggestions/${suggestionId}/skill-diff`,
    ),
  generateReport: (spaceId: string, suggestionIds: string[]) =>
    api.post<{ run: FeedbackRun }>(`${BASE}/spaces/${spaceId}/feedback/report`, {
      suggestion_ids: suggestionIds,
    }),
  reports: (spaceId: string) =>
    api.get<{ reports: FeedbackReport[] }>(
      `${BASE}/spaces/${spaceId}/feedback/reports`,
    ),
  publishReport: (spaceId: string, reportId: string) =>
    api.post<{ report: FeedbackReport }>(
      `${BASE}/spaces/${spaceId}/feedback/reports/${reportId}/publish`,
    ),
}

/** Template / workflow-template CRUD (ADR-0012): members can read, owner can write. */
export const templatesApi = {
  list: (spaceId: string) =>
    api.get<{ templates: Template[] }>(`${BASE}/spaces/${spaceId}/templates`),
  create: (
    spaceId: string,
    data: { name: string; description?: string; prompt: string; params: unknown[] },
  ) =>
    api.post<{ template: Template; warnings: string[] }>(
      `${BASE}/spaces/${spaceId}/templates`, data,
    ),
  update: (
    spaceId: string,
    id: string,
    data: { name?: string; description?: string; prompt?: string; params?: unknown[] },
  ) =>
    api.patch<{ template: Template; warnings: string[] }>(
      `${BASE}/spaces/${spaceId}/templates/${id}`, data,
    ),
  remove: (spaceId: string, id: string) =>
    api.delete<{ ok: boolean }>(`${BASE}/spaces/${spaceId}/templates/${id}`),
  listWorkflows: (spaceId: string) =>
    api.get<{ workflows: WorkflowTemplate[] }>(
      `${BASE}/spaces/${spaceId}/workflow-templates`,
    ),
  createWorkflow: (
    spaceId: string,
    data: { name: string; description?: string; nodes: { template_id: string }[] },
  ) =>
    api.post<{ workflow: WorkflowTemplate }>(
      `${BASE}/spaces/${spaceId}/workflow-templates`, data,
    ),
  updateWorkflow: (
    spaceId: string,
    id: string,
    data: { name?: string; description?: string; nodes?: { template_id: string }[] },
  ) =>
    api.patch<{ workflow: WorkflowTemplate }>(
      `${BASE}/spaces/${spaceId}/workflow-templates/${id}`, data,
    ),
  removeWorkflow: (spaceId: string, id: string) =>
    api.delete<{ ok: boolean }>(
      `${BASE}/spaces/${spaceId}/workflow-templates/${id}`,
    ),
}

export const sessionsApi = {
  list: (spaceId: string) =>
    api.get<{ sessions: Session[] }>(
      `${BASE}/sessions?space_id=${encodeURIComponent(spaceId)}`,
    ),
  get: (id: string) => api.get<{ session: Session }>(`${BASE}/sessions/${id}`),
  create: (
    spaceId: string,
    model?: string,
    opts?: {
      templateId?: string
      workflowTemplateId?: string
      params?: Record<string, string>
    },
  ) =>
    api.post<{ session: Session }>(`${BASE}/sessions`, {
      space_id: spaceId,
      ...(model ? { model } : {}),
      ...(opts?.templateId ? { template_id: opts.templateId } : {}),
      ...(opts?.workflowTemplateId
        ? { workflow_template_id: opts.workflowTemplateId }
        : {}),
      ...(opts?.params ? { params: opts.params } : {}),
    }),
  remove: (id: string) => api.delete<{ ok: boolean }>(`${BASE}/sessions/${id}`),
  sendMessage: (
    id: string,
    content: string,
    model?: string,
    effort?: string,
    taskId?: string,
  ) =>
    api.post<{ status: string }>(`${BASE}/sessions/${id}/messages`, {
      content,
      ...(model ? { model } : {}),
      ...(effort ? { effort } : {}),
      ...(taskId ? { task_id: taskId } : {}),
    }),
  answer: (
    id: string,
    questionId: string,
    answers: AnswerPayload,
    taskId?: string,
  ) =>
    api.post<{ status: string }>(`${BASE}/sessions/${id}/answer`, {
      question_id: questionId,
      answers,
      ...(taskId ? { task_id: taskId } : {}),
    }),
  cancel: (id: string, taskId?: string) =>
    api.post<{ ok: boolean }>(
      `${BASE}/sessions/${id}/cancel`,
      taskId ? { task_id: taskId } : {},
    ),
  advancePreview: (id: string) =>
    api.post<AdvancePreview>(`${BASE}/sessions/${id}/advance/preview`, {}),
  advanceConfirm: (
    id: string,
    body: { node_index: number; params: Record<string, string>; summary: string },
  ) =>
    api.post<{ status: string }>(`${BASE}/sessions/${id}/advance/confirm`, body),
  files: (id: string) => api.get<{ files: FileEntry[] }>(`${BASE}/sessions/${id}/files`),
  preview: (id: string) =>
    api.get<SandboxPreview>(`${BASE}/sessions/${id}/preview`),
  fileContent: (id: string, path: string) =>
    api.get<FileContent>(
      `${BASE}/sessions/${id}/files/content?path=${encodeURIComponent(path)}`,
    ),
  // Raw trace events moved to the admin console: see adminApi.rawEvents (/admin/sessions/{id}/raw-events).
}

/** Admin console (everything requires is_admin; the backend returns 404 for non-admins).
 * List endpoints paginate by offset/limit; responses wrap rows under a specific key + total/offset/limit. */
export const adminApi = {
  stats: () => api.get<AdminStats>(`${BASE}/admin/stats`),
  users: (q = '', offset = 0, limit = 50) =>
    api.get<{ users: AdminUser[] } & Paginated>(
      `${BASE}/admin/users?q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`,
    ),
  sessions: (
    filters: { user?: string; space_id?: string; status?: string } = {},
    offset = 0,
    limit = 50,
  ) => {
    const params = new URLSearchParams()
    if (filters.user) params.set('user', filters.user)
    if (filters.space_id) params.set('space_id', filters.space_id)
    if (filters.status) params.set('status', filters.status)
    params.set('offset', String(offset))
    params.set('limit', String(limit))
    return api.get<{ sessions: AdminSession[] } & Paginated>(
      `${BASE}/admin/sessions?${params.toString()}`,
    )
  },
  /** Raw trace events (root + full subtask tree). cursor is the {task_id: last_seq}
   *  map echoed from the previous response (seq counts independently per task stream);
   *  send it back verbatim for incremental fetches. */
  rawEvents: (id: string, cursor?: Record<string, number>) =>
    api.get<{ events: RawEnvelope[]; cursor: Record<string, number> }>(
      `${BASE}/admin/sessions/${id}/raw-events${
        cursor ? `?cursor=${encodeURIComponent(JSON.stringify(cursor))}` : ''
      }`,
    ),
  spaces: (offset = 0, limit = 50) =>
    api.get<{ spaces: AdminSpace[] } & Paginated>(
      `${BASE}/admin/spaces?offset=${offset}&limit=${limit}`,
    ),
  spaceMembers: (spaceId: string) =>
    api.get<{ members: SpaceMember[] }>(`${BASE}/admin/spaces/${spaceId}/members`),
  spaceKnowledge: (spaceId: string) =>
    api.get<{ sources: KnowledgeSource[] }>(
      `${BASE}/admin/spaces/${spaceId}/knowledge`,
    ),
  spaceSkills: (spaceId: string) =>
    api.get<{ skills: Skill[] }>(`${BASE}/admin/spaces/${spaceId}/skills`),
  config: () => api.get<{ items: AdminConfigItem[] }>(`${BASE}/admin/config`),
  putConfig: (key: string, value: boolean) =>
    api.put<{ item: AdminConfigItem }>(
      `${BASE}/admin/config/${encodeURIComponent(key)}`,
      { value },
    ),
}

export function eventsUrl(
  sessionId: string,
  sinceSeq: number,
  taskId?: string | null,
): string {
  let url = `${BASE}/sessions/${sessionId}/events?since_seq=${sinceSeq}`
  if (taskId) url += `&task_id=${encodeURIComponent(taskId)}`
  return url
}

/** Raw ContentStore bytes (Trace page derefs a ContentRef); the body is interpreted per ref.media_type. */
export function contentUrl(hash: string): string {
  return `${BASE}/content/${hash}`
}

/** Channels (space collaboration layer, ADR-0016). */
export const channelsApi = {
  list: (spaceId: string) =>
    api.get<{ channels: Channel[] }>(`${BASE}/spaces/${spaceId}/channels`),
  create: (spaceId: string, data: { name: string; description?: string }) =>
    api.post<{ channel: Channel }>(`${BASE}/spaces/${spaceId}/channels`, data),
  update: (
    channelId: string,
    data: { name?: string; description?: string; archived?: boolean },
  ) => api.patch<{ channel: Channel }>(`${BASE}/channels/${channelId}`, data),
  messages: (channelId: string, beforeSeq?: number, limit = 50) => {
    let url = `${BASE}/channels/${channelId}/messages?limit=${limit}`
    if (beforeSeq !== undefined) url += `&before_seq=${beforeSeq}`
    return api.get<{ messages: ChannelMessage[]; topics: ChannelTopic[] }>(url)
  },
  send: (channelId: string, text: string, mentionAgent: boolean) =>
    api.post<{ message: ChannelMessage; topic: ChannelTopic | null }>(
      `${BASE}/channels/${channelId}/messages`,
      { text, mention_agent: mentionAgent },
    ),
  topicMessage: (channelId: string, topicId: string, text: string) =>
    api.post<{ status: string }>(
      `${BASE}/channels/${channelId}/topics/${topicId}/messages`,
      { text },
    ),
  topicAnswer: (
    channelId: string,
    topicId: string,
    questionId: string,
    answers: AnswerPayload,
  ) =>
    api.post<{ status: string }>(
      `${BASE}/channels/${channelId}/topics/${topicId}/answer`,
      { question_id: questionId, answers },
    ),
  topicToCard: (channelId: string, topicId: string) =>
    api.post<{ card: BoardCard }>(
      `${BASE}/channels/${channelId}/topics/${topicId}/to-card`,
    ),
  markRead: (channelId: string, seq: number) =>
    api.put<{ ok: boolean }>(`${BASE}/channels/${channelId}/read`, { seq }),
}

/** Channel SSE stream URL (message frames carry id=seq; resume with lastSeq after a disconnect). */
export function channelStreamUrl(channelId: string, sinceSeq: number): string {
  return `${BASE}/channels/${channelId}/stream?since_seq=${sinceSeq}`
}

/** Task board (ADR-0016 Phase 2). */
export const boardApi = {
  list: (spaceId: string) =>
    api.get<{ cards: BoardCard[]; columns: string[] }>(
      `${BASE}/spaces/${spaceId}/board`,
    ),
  createCard: (
    spaceId: string,
    data: {
      title: string
      description?: string
      column_key?: string
      assignee?: string
      due_date?: string
    },
  ) => api.post<{ card: BoardCard }>(`${BASE}/spaces/${spaceId}/board/cards`, data),
  updateCard: (
    cardId: string,
    data: {
      title?: string
      description?: string
      column_key?: string
      position?: number
      assignee?: string
      due_date?: string
      clear_assignee?: boolean
      clear_due_date?: boolean
    },
  ) => api.patch<{ card: BoardCard }>(`${BASE}/board/cards/${cardId}`, data),
  removeCard: (cardId: string) =>
    api.delete<{ ok: boolean }>(`${BASE}/board/cards/${cardId}`),
  startSession: (cardId: string, templateId: string, params: Record<string, string>) =>
    api.post<{ card: BoardCard; session: Session }>(
      `${BASE}/board/cards/${cardId}/start-session`,
      { template_id: templateId, params },
    ),
}
