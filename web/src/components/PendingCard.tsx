import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, type Api } from "../api/client";
import type { Pending, Question } from "../api/types";
import { DiffView, EDITS, editDiff } from "./DiffView";
import { Markdown } from "./Markdown";

/** Answers an approval with one of its choices, as noeta.host.pending.answer_choice does. */
export async function answerChoice(api: Api, p: Pending, choice: string, message = ""): Promise<void> {
  if (choice === "deny") await api.answer(p.task_id, p.request_id, "deny", { data: message ? { message } : null });
  else if (choice === "always") await api.answer(p.task_id, p.request_id, "allow", { always: true });
  else if (choice.startsWith("mode:")) await api.answer(p.task_id, p.request_id, "allow", { data: { mode: choice.slice(5) } });
  else await api.answer(p.task_id, p.request_id, "allow");
}

/** Sends an answer: a failure is shown on the card; a conflict (answered
 * elsewhere) refreshes the cards instead. */
function useAnswer(done: () => void): [boolean, string, (send: () => Promise<unknown>) => Promise<void>] {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async (send: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await send();
      done();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError("This was already answered elsewhere.");
        done();
      } else setError(`The answer was not sent: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };
  return [busy, error, run];
}

/** Focuses the card when it appears, so its keys work at once. */
function useFocus<T extends HTMLElement>(focus: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (focus) ref.current?.focus();
  }, [focus]);
  return ref;
}

function ApprovalCard({ p, api, done, focus }: { p: Pending; api: Api; done: () => void; focus: boolean }) {
  const [feedback, setFeedback] = useState("");
  const [denying, setDenying] = useState(false);
  const [active, setActive] = useState(0);
  const [busy, error, run] = useAnswer(done);
  const ref = useFocus<HTMLDivElement>(focus);
  const choices = p.choices ?? [
    { id: "allow", label: "Yes" },
    { id: "deny", label: "No" },
  ];
  const pick = (id: string) => {
    if (busy) return;
    if (id === "deny" && !denying) {
      setDenying(true);
      return;
    }
    void run(() => answerChoice(api, p, id, feedback.trim()));
  };
  // Claude Code's dialog keys: a digit picks, arrows move, Enter takes the
  // highlighted choice, Esc says no.
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target instanceof HTMLTextAreaElement) {
      if (e.key === "Escape") void run(() => answerChoice(api, p, "deny", feedback.trim()));
      return;
    }
    const n = Number(e.key);
    if (Number.isInteger(n) && n >= 1 && n <= choices.length) pick(choices[n - 1].id);
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") setActive((a) => (a + (e.key === "ArrowDown" ? 1 : choices.length - 1)) % choices.length);
    else if (e.key === "Enter") pick(choices[active].id);
    else if (e.key === "Escape") void run(() => answerChoice(api, p, "deny"));
    else return;
    e.preventDefault();
  };
  const diff = EDITS.has(p.tool) ? editDiff(p.tool, p.input) : "";
  return (
    <div className="card approval" role="dialog" aria-label={p.title} tabIndex={-1} ref={ref} onKeyDown={onKey}>
      <div className="card-title">{p.title}</div>
      {p.prompt && <div className="card-prompt">{p.prompt}</div>}
      {p.tool === "Bash" && <pre className="command">$ {String(p.input.command ?? "")}</pre>}
      {diff && (
        <>
          <div className="muted">{String(p.input.file_path ?? p.input.notebook_path ?? "")}</div>
          <DiffView diff={diff} />
        </>
      )}
      <div className="card-question">Do you want to proceed?</div>
      <div className="choices">
        {choices.map((c, i) => (
          <button
            key={c.id}
            className={`choice${c.id === "deny" ? " deny" : ""}${i === active ? " active" : ""}`}
            disabled={busy}
            tabIndex={-1}
            onClick={() => pick(c.id)}
            onMouseEnter={() => setActive(i)}
          >
            <span className="key">{i + 1}</span> {c.label}
          </button>
        ))}
      </div>
      {denying && (
        <div className="deny-feedback">
          <textarea
            autoFocus
            placeholder="Tell noeta what to do differently (optional)"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                pick("deny");
              }
            }}
          />
          <button className="choice deny" disabled={busy} onClick={() => pick("deny")}>
            Deny{feedback.trim() ? " with feedback" : ""}
          </button>
        </div>
      )}
      {error && <div className="notice error">{error}</div>}
      <div className="muted keys">Esc to say no · 1–{choices.length} or ↑↓ Enter to choose</div>
    </div>
  );
}

type Answer = { selected?: string[]; other?: string };

function QuestionCard({ p, api, done, focus }: { p: Pending; api: Api; done: () => void; focus: boolean }) {
  const questions: Question[] = p.detail.questions ?? [];
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [busy, error, run] = useAnswer(done);
  const ref = useFocus<HTMLDivElement>(focus);
  const answered = (q: Question) => (answers[q.id]?.selected?.length ?? 0) > 0 || !!answers[q.id]?.other;
  const toggle = (q: Question, label: string) => {
    const cur = answers[q.id]?.selected ?? [];
    const selected = q.multiSelect ? (cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]) : [label];
    setAnswers({ ...answers, [q.id]: { selected } });
  };
  const other = (q: Question, text: string) => setAnswers({ ...answers, [q.id]: text ? { other: text } : {} });
  const complete = questions.every(answered);
  const submit = () => {
    if (complete && !busy) void run(() => api.answer(p.task_id, p.request_id, null, { data: { answers } }));
  };
  // Digits pick an option of the first question still open; Enter submits.
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target instanceof HTMLInputElement && e.target.type === "text") return;
    const q = questions.find((x) => !answered(x)) ?? questions[questions.length - 1];
    const n = Number(e.key);
    if (q && Number.isInteger(n) && n >= 1 && n <= (q.options ?? []).length) toggle(q, q.options![n - 1].label);
    else if (e.key === "Enter") submit();
    else return;
    e.preventDefault();
  };
  const plan = p.tool === "ExitPlanMode" ? String(p.input.plan ?? "") : "";
  return (
    <div className="card question" role="dialog" aria-label={p.title} tabIndex={-1} ref={ref} onKeyDown={onKey}>
      {plan ? (
        <>
          <div className="card-title">Ready to code? Here is the plan:</div>
          <div className="plan">
            <Markdown text={plan} />
          </div>
        </>
      ) : (
        p.agent && p.agent !== "main" && <div className="muted">from the {p.agent} agent</div>
      )}
      {questions.map((q) => (
        <fieldset key={q.id} className="q">
          <legend>
            {q.header && <span className="chip">{q.header}</span>} {q.question}
          </legend>
          {(q.options ?? []).map((o) => (
            <label key={o.label} className="option">
              <input
                type={q.multiSelect ? "checkbox" : "radio"}
                name={`q-${p.request_id}-${q.id}`}
                checked={answers[q.id]?.selected?.includes(o.label) ?? false}
                onChange={() => toggle(q, o.label)}
              />
              <span>
                {o.label}
                {o.description && <span className="muted"> — {o.description}</span>}
              </span>
            </label>
          ))}
          <input className="other" placeholder="Other…" value={answers[q.id]?.other ?? ""} onChange={(e) => other(q, e.target.value)} />
        </fieldset>
      ))}
      <button className="primary" disabled={!complete || busy} onClick={submit}>
        Submit
      </button>
      {error && <div className="notice error">{error}</div>}
    </div>
  );
}

/** The approvals and questions a session (or one of its sub-agents) waits on. */
export function PendingCards({ pending, api, answered }: { pending: Pending[]; api: Api; answered: () => void }) {
  return (
    <div className="pending">
      {pending.map((p, i) =>
        p.kind === "approval" ? (
          <ApprovalCard key={p.task_id + p.request_id} p={p} api={api} done={answered} focus={i === 0} />
        ) : (
          <QuestionCard key={p.task_id + p.request_id} p={p} api={api} done={answered} focus={i === 0} />
        ),
      )}
    </div>
  );
}
