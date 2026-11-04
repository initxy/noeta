// The JSON noeta serve speaks (noeta/core/events.py, noeta/core/types.py,
// noeta/host/_http_routes.py). Only the fields the app reads are typed.

export type Block =
  | { type: "text"; text: string }
  | { type: "thinking"; text?: string }
  | { type: "image"; media_type: string; data: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; call_id: string; content: Block[]; is_error?: boolean }
  | { type: "tool_reference"; tool_name: string };

export interface Message {
  role: "user" | "assistant" | "system";
  content: Block[];
  origin?: string | null;
}

export type InboxBody =
  | { kind: "message"; message: Message; when?: string }
  | { kind: "answer"; request_id: string; decision?: string | null; data?: unknown }
  | { kind: "cancel"; reason?: string; turn_only?: boolean }
  | { kind: "child_ended"; child_id: string; status: string; answer?: string; error?: string }
  | { kind: "external"; name: string; data?: unknown }
  | { kind: "timer"; name: string };

export interface InboxItem {
  seq: number;
  at: number;
  body: InboxBody;
}

export type Event =
  | { type: "TaskCreated"; agent: string; config: Record<string, unknown>; parent_id?: string | null }
  | { type: "InputTaken"; items: InboxItem[]; through: number }
  | { type: "ModelCalled"; model: string; attempt?: number }
  | {
      type: "ModelReplied";
      response: { message: Message; stop_reason: string; usage: Usage; model?: string };
    }
  | { type: "ToolStarted"; call: { id: string; name: string; input: Record<string, unknown> } }
  | {
      type: "ToolFinished";
      result: { call_id: string; content: Block[]; is_error?: boolean };
      meta?: Record<string, unknown>;
    }
  | { type: "Compacted"; summary: string; through: number }
  | { type: "Waiting"; on: string; ids?: string[]; detail?: Record<string, unknown> }
  | { type: "TaskEnded"; status: string; answer?: string; error?: string }
  | { type: "ExtensionRecorded"; extension: string; kind: string; data?: unknown; message?: Message | null };

export interface Recorded {
  seq: number;
  at: number;
  event: Event;
}

export interface Usage {
  uncached?: number;
  cache_read?: number;
  cache_write?: number;
  output?: number;
  reasoning?: number;
}

export interface Delta {
  kind: "text" | "thinking" | "block" | "tool";
  text?: string;
  call_seq?: number;
  attempt?: number;
  call_id?: string;
  tool?: string;
}

/** A root task as the sidebar lists it (`GET /summaries`). */
export interface TaskSummary {
  task_id: string;
  agent: string;
  status: "ready" | "running" | "waiting" | "ended";
  waiting_on: string | null;
  end_status: string | null;
  created_at: number;
  updated_at: number;
  title: string;
  cwd: string;
  prompt: string;
}

export interface Choice {
  id: string;
  label: string;
}

export interface Question {
  id: string;
  question: string;
  header?: string;
  multiSelect?: boolean;
  options?: { label: string; description?: string }[];
}

export interface Pending {
  task_id: string;
  request_id: string;
  kind: "approval" | "question";
  tool: string;
  input: Record<string, unknown>;
  prompt: string;
  detail: { questions?: Question[]; [key: string]: unknown };
  agent: string;
  title: string;
  choices?: Choice[];
}

export interface Command {
  name: string;
  description: string;
  argument_hint: string;
  local: boolean;
}

export interface ModelInfo {
  id: string;
  context_window: number;
  max_output_tokens: number;
  aliases: string[];
}

export interface TreeUsage {
  tasks: number;
  cost_usd: number;
  usage: Usage;
  by_model: Record<string, { usage: Usage; calls: number; cost_usd: number }>;
}

export interface RewindPoint {
  index: number;
  task_id: string;
  seq: number;
  at: number;
  text: string;
  files: string[];
}

/** A person's turn: text, or text and images (base64). */
export type Input =
  | string
  | { role: "user"; content: ({ type: "text"; text: string } | { type: "image"; media_type: string; data: string })[] };
