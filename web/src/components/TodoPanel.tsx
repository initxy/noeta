import type { Todo } from "../state/transcript";

const MARK: Record<string, string> = { completed: "☑", in_progress: "◐", pending: "☐" };

/** The session's task list, as the model keeps it with its task tools. */
export function TodoPanel({ todos }: { todos: Todo[] }) {
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
