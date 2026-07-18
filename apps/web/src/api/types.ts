/** REST + SSE wire-protocol types — one-to-one with the contract pinned in docs/implementation-specs. */

// ---- REST ----

export interface User {
  username: string
  email?: string
  email_prefix?: string
  name?: string
  avatar?: string
  /** Admin allowlist hit (returned by /auth/me): the frontend shows the admin console entry from this. */
  is_admin?: boolean
}

/** GET /auth/config: the login page configures its login UI from this. */
export interface AuthConfig {
  dev_login_enabled: boolean
}

export interface Session {
  id: string
  title: string
  model: string
  status?: string // idle | running | waiting (always returned by the backend to_api; used by the Trace Inspector)
  space_id: string // space the session belongs to
  created_at: number // Unix seconds
  updated_at: number // Unix seconds
  /** Template id when started from a single template (record only). */
  template_id?: string | null
  /** Whether this is a workflow session (list endpoint carries the flag only; detail carries the workflow view). */
  is_workflow?: boolean
  /** Workflow view (attached by GET /sessions/{id}; absent on the list endpoint). */
  workflow?: WorkflowView | null
}

// ---------------------------------------------------------------- Templates (ADR-0012)
export interface TemplateParam {
  name: string
  description: string
  required: boolean
}

/** Single-node template: prompt with {param} placeholders; params is the parameter definition list. */
export interface Template {
  id: string
  space_id: string
  name: string
  description: string
  prompt: string
  params: TemplateParam[]
  created_at: number
  updated_at: number
}

export interface WorkflowTemplateNode {
  template_id: string
  /** Referenced template name attached by the list endpoint; null when the reference is broken. */
  template_name?: string | null
}

/** Workflow template: an ordered node list; nodes reference single-node templates. */
export interface WorkflowTemplate {
  id: string
  space_id: string
  name: string
  description: string
  nodes: WorkflowTemplateNode[]
  created_at: number
  updated_at: number
}

/** View of a single node within a workflow session (workflow_update frame / session.workflow). */
export interface WorkflowNodeView {
  index: number
  name: string
  description: string
  params: TemplateParam[]
  task_id: string | null
  status: 'not_started' | 'idle' | 'running' | 'waiting'
}

/** Workflow session view (tab-bar data source; full idempotent snapshot). */
export interface WorkflowView {
  name: string
  workflow_template_id?: string | null
  nodes: WorkflowNodeView[]
  current_index: number | null
}

/** POST /sessions/{id}/advance/preview response: prefilled handoff params + handoff summary. */
export interface AdvancePreview {
  node_index: number
  node_name: string
  param_defs: TemplateParam[]
  params: Record<string, string | null>
  summary: string
  /** Degraded when the LLM is unavailable / parsing failed: an all-empty form the user fills in by hand. */
  degraded: boolean
}

/** Space: a default personal space (is_personal) plus creatable team spaces. */
export interface Space {
  id: string
  name: string
  description: string
  is_personal: boolean
  owner: string
  my_role: 'owner' | 'member'
  member_count: number
  created_at: number
  updated_at: number
}

export interface SpaceMember {
  username: string
  name: string | null
  avatar: string | null
  email: string | null
  role: 'owner' | 'member'
  added_by: string | null
  created_at: number
}

export interface SpaceDetail extends Space {
  members: SpaceMember[]
}

/** GET/PUT /spaces/{id}/agent-config: space-level agent configuration. */
export interface AgentConfig {
  /** Appended persona segment (assembled into the session workspace AGENT.md). */
  prompt: string
  /** Memory toggle (a reserved field until the memory feature ships). */
  memory_enabled: boolean
  /** Knowledge-source ids that take part in assembly; null = all. */
  knowledge_sources: string[] | null
  /** Empty = platform default. */
  default_model: string
  default_effort: string
}

export interface ModelInfo {
  id: string
  label: string
  default?: boolean
  efforts?: string[]
  default_effort?: string
}

/** GET /spaces/{id}/skills: single authoritative list — builtin + space skills (upload / market)
 * returned together. source=builtin is read-only; upload is user-uploaded; market was installed
 * from a skill market (carries version / remote_id metadata). Existence is owned by the backend
 * space_skills table; the frontend no longer merges two lists. */
export interface Skill {
  name: string
  description: string
  source: 'builtin' | 'upload' | 'market'
  /** Enabled toggle; disabled skills are not assembled into new sessions. Space list = space
   * scope; admin /skills = platform scope (global builtin toggle). Always present on the space
   * list endpoint; builtins shown read-only there, always true. */
  enabled?: boolean
  /** User-defined group name (display-layer organization only); null for ungrouped / builtin. */
  group?: string | null
  /** Only present for source=market: version, market remote_id, install time, auto-update flag. */
  version?: string
  remote_id?: string
  installed_at?: number
  auto_update?: boolean
}

/** Skill preview file-tree entry (returned by preview without a path). */
export interface PreviewEntry {
  path: string
  size: number
  is_dir: boolean
}

/** Skill preview file content (returned by preview with a path). */
export interface PreviewContent {
  path: string
  content: string
  truncated: boolean
  binary: boolean
}

/** Knowledge source: a git repository or a local directory, materialized into
 * shared_data_dir for agent retrieval. */
export interface KnowledgeSource {
  id: string
  space_id: string
  name: string
  type: 'git_repo' | 'local_dir'
  config: Record<string, unknown>
  status: 'pending' | 'syncing' | 'ready' | 'failed'
  last_sync_at: number | null
  last_error: string | null
  created_by: string
  created_at: number
  updated_at: number
  /** Imported document count (backend writes config.doc_count back on sync completion; null before a sync). */
  doc_count?: number | null
  /** Export-failure count of the most recent sync (config.failed_count cache; null before a sync). */
  failed_count?: number | null
}

/** MCP connector: a per-space MCP server config (http or stdio transport).
 * Credential VALUES never reach the client — the backend echoes header/env
 * NAMES only (header_names / env_names). */
export interface McpConnector {
  space_id: string
  alias: string
  type: 'http' | 'stdio'
  url: string
  /** Names of the configured request headers (values stay server-side). */
  header_names: string[]
  command: string
  args: string[]
  /** Names of the configured env variables (values stay server-side). */
  env_names: string[]
  enabled: boolean
  /** Enabled-tool subset (raw tool names); null = all advertised tools. */
  tools: string[] | null
  created_by: string
  created_at: number
  updated_at: number
}

/** One entry of a connector's advertised tool menu (discovery). */
export interface McpToolInfo {
  name: string
  description: string
}

/** One entry of a connector's prompt menu (discovery). */
export interface McpPromptInfo {
  name: string
  noeta_name: string
  description: string
  arguments: { name: string; description?: string; required?: boolean }[]
}

/** One entry of a connector's static-resource menu (discovery). */
export interface McpResourceInfo {
  uri: string
  name: string
  description: string
  mime_type: string
  noeta_ref: string
}

/** Sync progress (in-memory state, exposed via sync status while syncing; null otherwise).
 * Phases: starting / cloned / fetched / copying / done, with optional per-phase fields. */
export interface SyncProgress {
  phase: string
  existing?: boolean
  file_count?: number
  commit?: string
}

/** Space memory entry (agent long-term memory; an index row of the markdown file pool). */
export interface MemoryEntry {
  name: string
  /** Frontmatter description (falls back to the first body line when missing). */
  description: string
  /** user / project / procedural / reference; may be an empty string for legacy files. */
  type: string
  updated_at: number | null
}

/** A single message-level feedback entry (thumbs up/down, ADR-0017). */
export interface FeedbackEntry {
  id: string
  space_id: string
  session_id: string
  task_id: string
  event_seq: number | null
  author: string
  /** 1 = thumbs up / -1 = thumbs down */
  rating: number
  tags: string[]
  comment: string
  /** none | text */
  reference_kind: string
  reference_origin_url: string | null
  analyzed_run_id: string | null
  created_at: number
  updated_at: number
}

export interface FeedbackCounts {
  positive: number
  negative: number
}

/** Structured improvement suggestion produced by the analysis agent (evidence required). */
export interface FeedbackSuggestion {
  id: string
  space_id: string
  run_id: string
  /** memory | skill | report */
  channel: string
  title: string
  body: string
  skill_name: string | null
  /** Full modified SKILL.md text (non-null for the skill channel on a space skill; one-click apply). */
  skill_patch: string | null
  evidence: { feedback_id: string; note: string }[]
  /** pending | adopted | dismissed */
  status: string
  adopted_result: { memory?: string; skill?: string; backup?: string } | null
  created_at: number
  decided_at: number | null
  decided_by: string | null
}

/** Lifecycle of one feedback-analysis run. */
export interface FeedbackRun {
  id: string
  space_id: string
  /** analysis (attribution) | report (report aggregation) */
  kind: string
  /** running | done | failed */
  status: string
  triggered_by: string
  task_id: string | null
  error: string | null
  started_at: number
  finished_at: number | null
}

/** Artifact of a report-mode run: a markdown draft; publishing backfills doc_url
 * with the server-side markdown file path (rendered as a path string, not a link). */
export interface FeedbackReport {
  id: string
  space_id: string
  run_id: string
  title: string
  body: string
  /** draft | published */
  status: string
  doc_url: string | null
  created_by: string
  created_at: number
  published_at: number | null
}

/** One item of the citation-path resolution result (POST resolve-paths):
 * a knowledge/ path appearing in an AI reply → title / origin link / excerpt.
 * exists=false degrades to plain text on the frontend; anchor_found=false
 * (file present, anchor heading gone) degrades to a document-level chip
 * plus a "source has been updated" note. */
export interface ResolvedKnowledgePath {
  /** Normalized path (anchor stripped): knowledge/<source name>/<relative path> */
  path: string
  anchor: string | null
  exists: boolean
  /** Anchor lookup result when an anchor was given; null when no anchor. */
  anchor_found: boolean | null
  source_name: string
  source_type: 'git_repo' | 'local_dir' | null
  title: string | null
  /** Origin URL of the source document (null for files without frontmatter → chip not clickable). */
  origin_url: string | null
  /** Excerpt sliced by heading when the anchor was found (for the hover card). */
  excerpt: string | null
}

export interface FileEntry {
  path: string
  size: number
  mtime: number
}

export interface FileContent {
  path: string
  content: string
  truncated: boolean
}

/** GET /sessions/{id}/preview: sandbox live-preview discovery (panel reverse proxy on an isolated origin). */
export interface SandboxPreview {
  token: string
  port: number | null
  panels: {
    browser: string
    terminal: string
    code: string
  }
}

/** GET /admin/sessions/{id}/raw-events: raw noeta envelopes (for the Trace page,
 *  root + full subtask tree; seq is only monotonic within each task stream). */
export interface RawEnvelope {
  id: string
  task_id: string
  seq: number
  type: string
  schema_version: number
  occurred_at: number // Unix seconds (float)
  actor: string
  trace_id: string
  correlation_id: string
  causation_id: string | null
  payload: unknown
  origin: string
}

// ---- Admin console ----

/** GET /admin/stats: platform entity-count overview. */
export interface AdminStats {
  users: number
  spaces: number
  sessions: { total: number; by_status: Record<string, number> }
  knowledge_sources: { total: number; by_status: Record<string, number> }
  builtin_skills: number
  space_skills: number
}

/** GET /admin/users row: user profile + registration / activity timestamps. */
export interface AdminUser {
  username: string
  email: string | null
  name: string | null
  avatar: string | null
  created_at: number
  updated_at: number
}

/** GET /admin/sessions row: session (task) + owning user / space name. */
export interface AdminSession {
  id: string
  title: string
  model: string
  status?: string
  space_id: string
  space_name: string | null
  user: string
  created_at: number
  updated_at: number
}

/** GET /admin/spaces row: space + member-count / session-count aggregates. */
export interface AdminSpace {
  id: string
  name: string
  description: string
  is_personal: boolean
  owner: string
  member_count: number
  session_count: number
  created_at: number
  updated_at: number
}

/** GET /admin/config row: default value / effective value / override metadata of a registered config key. */
export interface AdminConfigItem {
  key: string
  type: string
  description: string
  value: boolean
  default: boolean
  overridden: boolean
  updated_by: string | null
  updated_at: number | null
}

/** Common pagination fields (each admin list endpoint wraps rows under a specific key + these three fields). */
export interface Paginated {
  total: number
  offset: number
  limit: number
}

// ---- SSE events (the backend deterministically maps noeta envelopes to UI events) ----

export interface QuestionChoice {
  id: string
  label: string
  description?: string | null
}

export interface QuestionItem {
  id: string
  question: string
  header?: string | null
  choices?: QuestionChoice[] | null
  allow_freeform?: boolean
}

export type TurnStatus = 'awaiting_input' | 'completed' | 'cancelled' | 'failed'

/** A single checklist item of the todo_write capability (three states validated by noeta). */
export interface TodoItem {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

/** The four memory operations (folded by the translator per noeta tool name; see backend _MEMORY_TOOL_OPS). */
export type MemoryOp = 'write' | 'read' | 'search' | 'archive'

/** Composer image attachment as sent in the message request body (base64-encoded). */
export interface ImageAttachment {
  media_type: string
  data_base64: string
}

/** An image attached to a user turn, as exposed by the user_message event:
 * a ContentRef handle — the bytes are fetched via GET /content/{hash}. */
export interface UserMessageImage {
  hash: string
  media_type: string
}

export type UIEvent =
  | {
      type: 'user_message'
      data: { content: string; images?: UserMessageImage[] }
    }
  | { type: 'assistant_text'; data: { text: string } }
  | { type: 'thinking'; data: { text: string } }
  | {
      type: 'tool_call'
      data: {
        call_id: string
        tool_name: string
        arguments: unknown
        /** Attached when the event comes from a subtask stream (SSE frame seq is null; excluded from replay). */
        subtask_id?: string
      }
    }
  | {
      type: 'tool_result'
      data: {
        call_id: string
        success: boolean
        summary: string
        output: string
        subtask_id?: string
      }
    }
  | { type: 'skill_activated'; data: { skill: string } }
  | { type: 'todo_update'; data: { todos: TodoItem[] } }
  | {
      type: 'subtask_started'
      data: { subtask_id: string; agent_name: string; goal: string }
    }
  | {
      type: 'subtask_finished'
      data: {
        subtask_id: string
        status: 'completed' | 'failed' | 'cancelled'
        summary: string
      }
    }
  | {
      type: 'memory_op'
      // name holds the operation target: memory name for write/read/archive, query string for search
      data: { call_id: string; op: MemoryOp; name: string }
    }
  | {
      type: 'question'
      data: {
        question_id: string
        reason?: string | null
        questions: QuestionItem[]
      }
    }
  | { type: 'question_answered'; data: { question_id: string } }
  /**
   * Summary compaction landed: early history was folded into one summary. The chat
   * page renders a lightweight divider (mirroring Claude Code's "Context compacted");
   * details live on the Trace page.
   */
  | { type: 'compaction'; data: { replaced_count: number } }
  | { type: 'turn_started'; data: Record<string, never> }
  | { type: 'turn_finished'; data: { status: TurnStatus } }
  | { type: 'error'; data: { message: string } }
  | { type: 'replay_done'; data: Record<string, never> }
  | { type: 'workflow_update'; data: WorkflowView }
  /**
   * Token-streaming delta frame (ADR token-streaming-projection): a transient
   * projection — never written to the EventLog, never replayed, not backfilled on
   * reconnect; the durable truth is the assistant_text that follows. Multiple LLM
   * calls within one turn are distinguished by call_id; a new call_id = a new call
   * begins (buffer replaced wholesale). kind=thinking is chain-of-thought delta,
   * rendered de-emphasized.
   */
  | {
      type: 'delta'
      data: {
        call_id: string
        kind: 'text' | 'thinking'
        text: string
        index: number
      }
    }
  /**
   * A transient LLM failure (429 / network flap) about to back off and retry: the
   * frontend clears the streaming buffer of the same call_id (the retry re-streams
   * under the same call_id; without clearing, old and new half-streams would
   * concatenate into garbage). Observability event only, no UI bar (a later series
   * may add an explicit "retrying" hint).
   */
  | { type: 'llm_retry'; data: { call_id: string } }
  /**
   * Session metadata update (task D): pushed as a synthetic frame when the
   * LLM-generated title lands after the first turn (seq=null, not replayed — after
   * a refresh the sessions API already returns the new title). The frontend updates
   * the current session object + sidebar list title in place without refetching.
   */
  | { type: 'session_meta'; data: { title: string } }

export type UIEventType = UIEvent['type']

export interface SSEFrame {
  /** null = synthetic event (no id line; excluded from replay dedup). */
  seq: number | null
  event: UIEvent
}

/** The answers field of POST /sessions/{id}/answer: qid → single choice_id or free text. */
export type AnswerPayload = Record<string, { choice_id?: string; text?: string }>

// ---------------------------------------------------------- Channels (ADR-0016)

export interface Channel {
  id: string
  space_id: string
  name: string
  description: string
  /** Persistent channel session (lazily created on the first @Agent topic; used by the topic-panel SSE). */
  session_id: string | null
  created_by: string
  archived: boolean
  created_at: number
  updated_at: number
  /** Unread count (attached by the list endpoint; one's own messages don't count). */
  unread: number
}

export interface ChannelMessage {
  seq: number
  channel_id: string
  author: string
  text: string
  /** Non-null = topic root message (rendered as a topic card). */
  topic_id: string | null
  created_at: number
}

export interface ChannelTopic {
  id: string
  channel_id: string
  root_message_seq: number
  node_index: number
  created_by: string
  last_reply_preview: string
  created_at: number
  updated_at: number
  /** Topic root task (null while the seed is in flight); used by the topic-panel per-task SSE. */
  task_id: string | null
  /** running / waiting / idle (same semantics as session_tasks). */
  status: string
}

/** Channel SSE frame (GET /channels/{id}/stream). */
export type ChannelEvent =
  | { type: 'message'; data: ChannelMessage }
  | { type: 'topic_update'; data: ChannelTopic }
  | { type: 'topics_snapshot'; data: { topics: ChannelTopic[] } }
  | { type: 'replay_done'; data: Record<string, never> }

// ---------------------------------------------------- Task board (ADR-0016 P2)

export type BoardColumnKey = 'todo' | 'doing' | 'done'

export interface BoardCardLink {
  type: 'topic' | 'session'
  id: string
  label: string
  /** Owning channel when type=topic (for navigation). */
  channel_id?: string
}

export interface BoardCard {
  id: string
  space_id: string
  column_key: BoardColumnKey
  title: string
  description: string
  assignee: string | null
  due_date: string | null
  links: BoardCardLink[]
  position: number
  created_by: string
  created_at: number
  updated_at: number
}
