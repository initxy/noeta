import { useState } from "react";
import type { Api } from "../api/client";
import type { Pending, Question } from "../api/types";
import { Markdown } from "./Markdown";

/** Answers an approval with one of its choices, as noeta.host.pending.answer_choice does. */
export async function answerChoice(api: Api, p: Pending, choice: string, message = ""): Promise<void> {
  if (choice === "deny") await api.answer(p.task_id, p.request_id, "deny", { data: message ? { message } : null });
  else if (choice === "always") await api.answer(p.task_id, p.request_id, "allow", { always: true });
  else if (choice.startsWith("mode:")) await api.answer(p.task_id, p.request_id, "allow", { data: { mode: choice.slice(5) } });
  else await api.answer(p.task_id, p.request_id, "allow");
}

function ApprovalCard({ p, api, done }: { p: Pending; api: Api; done: () => void }) {
  const [feedback, setFeedback] = useState("");
  const [denying, setDenying] = useState(false);
  const [busy, setBusy] = useState(false);
  const choices = p.choices ?? [
    { id: "allow", label: "Yes" },
    { id: "deny", label: "No" },
  ];
  const pick = async (id: string) => {
    if (id === "deny" && !denying) {
      setDenying(true);
      return;
    }
    setBusy(true);
    try {
      await answerChoice(api, p, id, feedback.trim());
      done();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card approval" role="dialog" aria-label={p.title}>
      <div className="card-title">{p.title}</div>
      {p.prompt && <div className="card-prompt">{p.prompt}</div>}
      {p.tool === "Bash" && <pre className="command">$ {String(p.input.command ?? "")}</pre>}
      <div className="card-question">Do you want to proceed?</div>
      <div className="choices">
        {choices.map((c, i) => (
          <button key={c.id} className={c.id === "deny" ? "choice deny" : "choice"} disabled={busy} onClick={() => pick(c.id)}>
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
                void pick("deny");
              }
            }}
          />
          <button className="choice deny" disabled={busy} onClick={() => pick("deny")}>
            Deny{feedback.trim() ? " with feedback" : ""}
          </button>
        </div>
      )}
    </div>
  );
}

type Answer = { selected?: string[]; other?: string };

function QuestionCard({ p, api, done }: { p: Pending; api: Api; done: () => void }) {
  const questions: Question[] = p.detail.questions ?? [];
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [busy, setBusy] = useState(false);
  const toggle = (q: Question, label: string) => {
    const cur = answers[q.id]?.selected ?? [];
    const selected = q.multiSelect ? (cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]) : [label];
    setAnswers({ ...answers, [q.id]: { selected } });
  };
  const other = (q: Question, text: string) => setAnswers({ ...answers, [q.id]: text ? { other: text } : {} });
  const complete = questions.every((q) => (answers[q.id]?.selected?.length ?? 0) > 0 || answers[q.id]?.other);
  const submit = async () => {
    setBusy(true);
    try {
      await api.answer(p.task_id, p.request_id, null, { data: { answers } });
      done();
    } finally {
      setBusy(false);
    }
  };
  const plan = p.tool === "ExitPlanMode" ? String(p.input.plan ?? "") : "";
  return (
    <div className="card question" role="dialog" aria-label={p.title}>
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
    </div>
  );
}

/** The approvals and questions a session (or one of its sub-agents) waits on. */
export function PendingCards({ pending, api, answered }: { pending: Pending[]; api: Api; answered: () => void }) {
  return (
    <div className="pending">
      {pending.map((p) =>
        p.kind === "approval" ? (
          <ApprovalCard key={p.task_id + p.request_id} p={p} api={api} done={answered} />
        ) : (
          <QuestionCard key={p.task_id + p.request_id} p={p} api={api} done={answered} />
        ),
      )}
    </div>
  );
}
