// The todos plugin's web part: the session's task list beside the
// conversation, folded from the plugin's records (`created`, `updated`,
// `deleted`), and its task tools drawn as one line each.
import { ToolOutput } from "@noeta-ai/ui";
import { SLOTS, toolSlot, useAllRecords, type PluginContext, type PluginRecord, type ToolSlotProps } from "@noeta-ai/web-sdk";

export interface Todo {
  id: string;
  subject: string;
  activeForm?: string;
  status: string;
}

/** The task tools this plugin offers. */
export const TOOLS = ["TaskCreate", "TaskGet", "TaskUpdate", "TaskList"];

const text = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

/** The task list the records leave, in the order the tasks were made. */
export function todosOf(records: PluginRecord[]): Todo[] {
  let todos: Todo[] = [];
  for (const { kind, data } of records) {
    const d = (data ?? {}) as Record<string, unknown>;
    const id = String(d.id);
    if (kind === "created") {
      todos = [...todos.filter((t) => t.id !== id), { id, subject: String(d.subject ?? ""), activeForm: text(d.active_form), status: "pending" }];
    } else if (kind === "updated") {
      const f = (d.fields ?? {}) as Record<string, unknown>;
      todos = todos.map((t) =>
        t.id === id ? { ...t, subject: text(f.subject) ?? t.subject, activeForm: text(f.active_form) ?? t.activeForm, status: text(f.status) ?? t.status } : t,
      );
    } else if (kind === "deleted") {
      todos = todos.filter((t) => t.id !== id);
    }
  }
  return todos;
}

const MARK: Record<string, string> = { completed: "☑", in_progress: "◐", pending: "☐" };

export function TodoList({ todos }: { todos: Todo[] }) {
  if (!todos.length) return null;
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <aside className="todos" aria-label="Tasks">
      <div className="todos-head">
        Tasks <span className="muted">{done}/{todos.length}</span>
      </div>
      <ul>
        {todos.map((t) => (
          <li key={t.id} className={`todo todo-${t.status}`}>
            <span className="mark">{MARK[t.status] ?? "☐"}</span>
            {t.status === "in_progress" && t.activeForm ? t.activeForm : t.subject}
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** A task tool's call: one line, its output once opened. */
function TaskTool({ call, open }: ToolSlotProps) {
  return open && call.result !== undefined ? <ToolOutput text={call.result} error={call.status === "error"} open /> : null;
}

export function activate(ctx: PluginContext): void {
  const Panel = () => <TodoList todos={todosOf(useAllRecords(ctx))} />;
  ctx.slots.add(SLOTS.sidebar, { key: "todos", mount: "keyed", order: -10 }, Panel);
  for (const tool of TOOLS) ctx.slots.add(toolSlot(tool), { mount: "single" }, TaskTool);
}
