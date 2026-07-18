import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { templatesApi } from '../api/endpoints'
import type {
  Template,
  TemplateParam,
  WorkflowTemplate,
} from '../api/types'
import { cn } from '../lib/cn'
import { extractPlaceholders, splitPrompt } from '../lib/templatePrompt'
import { useSpace } from '../state/space'
import { useToast } from '../state/toast'
import {
  IconClose,
  IconEdit,
  IconFile,
  IconGit,
  IconPlus,
  IconTrash,
} from './icons'

/**
 * Templates page (ADR-0012): space-level management of single-node templates +
 * workflow templates.
 *
 * - Single-node template: name + description + prompt; params are auto-extracted
 *   from the {placeholders} in the prompt.
 * - Workflow template: an ordered node list; nodes reference single-node templates.
 * - Members are read-only; owners can create / edit / delete (the backend 403s as
 *   the second line of defense).
 */

const MAX_PARAMS = 20
const PROMPT_MAX_LEN = 32000

export function TemplatesPage({
  autoNew,
  onAutoNewDone,
}: {
  /** Open the "new template" editor right after mount (used by the hero start area); reset via callback once consumed. */
  autoNew?: boolean
  onAutoNewDone?: () => void
}) {
  const { currentSpace, currentSpaceId } = useSpace()
  const { toast } = useToast()
  const isOwner = currentSpace?.my_role === 'owner'

  const [templates, setTemplates] = useState<Template[]>([])
  const [workflows, setWorkflows] = useState<WorkflowTemplate[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<Template | 'new' | null>(null)
  const [editingWf, setEditingWf] = useState<WorkflowTemplate | 'new' | null>(null)

  const reload = useCallback(async () => {
    if (!currentSpaceId) return
    setLoading(true)
    try {
      const [t, w] = await Promise.all([
        templatesApi.list(currentSpaceId),
        templatesApi.listWorkflows(currentSpaceId),
      ])
      setTemplates(t.templates)
      setWorkflows(w.workflows)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load templates')
    } finally {
      setLoading(false)
    }
  }, [currentSpaceId, toast])

  useEffect(() => {
    void reload()
  }, [reload])

  // Hero "create template" entry: open the editor on arrival (owner only; members
  // have no create permission).
  useEffect(() => {
    if (!autoNew) return
    if (isOwner) setEditing('new')
    onAutoNewDone?.()
  }, [autoNew, isOwner, onAutoNewDone])

  const removeTemplate = async (id: string) => {
    if (!currentSpaceId) return
    try {
      await templatesApi.remove(currentSpaceId, id)
      await reload()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  const removeWorkflow = async (id: string) => {
    if (!currentSpaceId) return
    try {
      await templatesApi.removeWorkflow(currentSpaceId, id)
      await reload()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Header: same layout as the skills / knowledge pages (h1 + description). */}
        <h1 className="text-[20px] font-semibold text-ink">Templates</h1>
        <p className="mt-2 text-[13px] leading-relaxed text-ink-3">
          Capture recurring instructions as templates: a single template starts a
          session directly, and several templates chain into a workflow.
        </p>

        {/* ------------------------------------------------ Single-node templates */}
        <section className="mt-8">
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <div className="flex items-baseline gap-2">
                <h2 className="text-[15px] font-semibold text-ink">Templates</h2>
                {!loading && templates.length > 0 && (
                  <span className="font-mono text-[11px] text-ink-3">
                    {templates.length}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-[12px] text-ink-3">
                An instruction prompt (with {'{param}'} placeholders) that starts a
                session on its own or serves as a workflow node.
              </p>
            </div>
            {isOwner && (
              <button
                type="button"
                onClick={() => setEditing('new')}
                className="flex h-7 shrink-0 items-center gap-1 rounded-lg bg-accent px-2.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90"
              >
                <IconPlus className="h-3.5 w-3.5" />
                New template
              </button>
            )}
          </div>
          {loading ? (
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-28 animate-pulse rounded-xl bg-surface-2" />
              ))}
            </div>
          ) : templates.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border py-10 text-center">
              <IconFile className="mx-auto h-5 w-5 text-ink-3" />
              <p className="mt-2 text-[12.5px] text-ink-3">
                No templates yet
                {isOwner ? ' — create the first one to capture a recurring task.' : '.'}
              </p>
              {isOwner && (
                <button
                  type="button"
                  onClick={() => setEditing('new')}
                  className="mt-3 inline-flex h-7 items-center gap-1 rounded-lg bg-accent px-2.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90"
                >
                  <IconPlus className="h-3.5 w-3.5" />
                  New template
                </button>
              )}
            </div>
          ) : (
            <ul className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
              {templates.map((t) => (
                <li
                  key={t.id}
                  className="group flex flex-col rounded-xl border border-border bg-surface p-4 transition-colors hover:border-border-strong"
                >
                  <div className="flex items-center gap-2">
                    <IconFile className="h-4 w-4 shrink-0 text-accent" />
                    <p className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">
                      {t.name}
                    </p>
                    {isOwner && (
                      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition focus-within:opacity-100 group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={() => setEditing(t)}
                          title="Edit template"
                          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-ink"
                        >
                          <IconEdit className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => void removeTemplate(t.id)}
                          title="Delete template"
                          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 hover:bg-danger-soft hover:text-danger"
                        >
                          <IconTrash className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                  <p className="mt-1.5 flex-1 line-clamp-2 text-[12px] leading-relaxed text-ink-3">
                    {t.description || '(no description)'}
                  </p>
                  {/* Param chips share the prompt editor's placeholder highlight color:
                      one concept, one color across the app. */}
                  <div className="mt-2.5 flex flex-wrap items-center gap-1">
                    {t.params.length > 0 ? (
                      t.params.map((p) => (
                        <span
                          key={p.name}
                          title={p.description || undefined}
                          className="rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[10.5px] text-accent"
                        >
                          {`{${p.name}}`}
                        </span>
                      ))
                    ) : (
                      <span className="font-mono text-[10.5px] text-ink-3">
                        No params · runs on start
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ------------------------------------------------ Workflow templates */}
        <section className="mt-10">
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <div className="flex items-baseline gap-2">
                <h2 className="text-[15px] font-semibold text-ink">Workflows</h2>
                {!loading && workflows.length > 0 && (
                  <span className="font-mono text-[11px] text-ink-3">
                    {workflows.length}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-[12px] text-ink-3">
                Chain templates into a multi-stage workflow; advancing hands the
                previous stage's output over automatically.
              </p>
            </div>
            {isOwner && (
              <button
                type="button"
                onClick={() => setEditingWf('new')}
                disabled={templates.length === 0}
                title={templates.length === 0 ? 'Create at least one template first' : undefined}
                className="flex h-7 shrink-0 items-center gap-1 rounded-lg bg-accent px-2.5 text-[12.5px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <IconPlus className="h-3.5 w-3.5" />
                New workflow
              </button>
            )}
          </div>
          {loading ? (
            <div className="space-y-2.5">
              {[0, 1].map((i) => (
                <div key={i} className="h-24 animate-pulse rounded-xl bg-surface-2" />
              ))}
            </div>
          ) : workflows.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border py-10 text-center">
              <IconGit className="mx-auto h-5 w-5 text-ink-3" />
              <p className="mt-2 text-[12.5px] text-ink-3">
                No workflows yet
                {isOwner
                  ? templates.length === 0
                    ? ' — create templates first, then chain them.'
                    : ' — chain templates into a multi-stage flow.'
                  : '.'}
              </p>
            </div>
          ) : (
            <ul className="space-y-2.5">
              {workflows.map((w) => (
                <li
                  key={w.id}
                  className="group rounded-xl border border-border bg-surface p-4 transition-colors hover:border-border-strong"
                >
                  <div className="flex items-center gap-2">
                    <IconGit className="h-4 w-4 shrink-0 text-accent" />
                    <p className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">
                      {w.name}
                    </p>
                    <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
                      {w.nodes.length} {w.nodes.length === 1 ? 'node' : 'nodes'}
                    </span>
                    {isOwner && (
                      <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition focus-within:opacity-100 group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={() => setEditingWf(w)}
                          title="Edit workflow"
                          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-ink"
                        >
                          <IconEdit className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => void removeWorkflow(w.id)}
                          title="Delete workflow"
                          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 hover:bg-danger-soft hover:text-danger"
                        >
                          <IconTrash className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                  {w.description && (
                    <p className="mt-1.5 text-[12px] leading-relaxed text-ink-3">
                      {w.description}
                    </p>
                  )}
                  {/* Node chain: numbered chips + connecting lines express the run order (order is the semantics). */}
                  <div className="mt-2.5 flex flex-wrap items-center gap-y-1.5">
                    {w.nodes.map((n, i) => (
                      <span key={i} className="flex items-center">
                        {i > 0 && (
                          <span className="mx-1 h-px w-3.5 shrink-0 bg-border-strong" />
                        )}
                        <span
                          className={cn(
                            'flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-[11px]',
                            n.template_name
                              ? 'border-border bg-surface-2 text-ink-2'
                              : 'border-danger/40 bg-danger-soft text-danger',
                          )}
                        >
                          <span
                            className={cn(
                              'text-[10px]',
                              n.template_name ? 'text-ink-3' : 'text-danger',
                            )}
                          >
                            {i + 1}
                          </span>
                          {n.template_name ?? 'Missing template'}
                        </span>
                      </span>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {editing && currentSpaceId && (
        <TemplateEditor
          spaceId={currentSpaceId}
          template={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void reload()
          }}
        />
      )}
      {editingWf && currentSpaceId && (
        <WorkflowEditor
          spaceId={currentSpaceId}
          workflow={editingWf === 'new' ? null : editingWf}
          templates={templates}
          onClose={() => setEditingWf(null)}
          onSaved={() => {
            setEditingWf(null)
            void reload()
          }}
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------ Template editor
function TemplateEditor({
  spaceId,
  template,
  onClose,
  onSaved,
}: {
  spaceId: string
  template: Template | null
  onClose: () => void
  onSaved: () => void
}) {
  const { toast } = useToast()
  const [name, setName] = useState(template?.name ?? '')
  const [description, setDescription] = useState(template?.description ?? '')
  const [prompt, setPrompt] = useState(template?.prompt ?? '')
  // Param metadata (description / required) remembered by name: deleting a
  // placeholder and typing it back keeps the configuration.
  const [paramMeta, setParamMeta] = useState<
    Record<string, { description: string; required: boolean }>
  >(() =>
    Object.fromEntries(
      (template?.params ?? []).map((p) => [
        p.name,
        { description: p.description, required: p.required },
      ]),
    ),
  )
  const [submitting, setSubmitting] = useState(false)
  const [warnings, setWarnings] = useState<string[]>([])

  // The param list is extracted live from the prompt's {placeholders}; order = first appearance.
  const params: TemplateParam[] = useMemo(
    () =>
      extractPlaceholders(prompt).map((n) => ({
        name: n,
        description: paramMeta[n]?.description ?? '',
        required: paramMeta[n]?.required ?? false,
      })),
    [prompt, paramMeta],
  )
  const tooMany = params.length > MAX_PARAMS

  const setMeta = (
    n: string,
    patch: Partial<{ description: string; required: boolean }>,
  ) =>
    setParamMeta((m) => {
      const cur = m[n] ?? { description: '', required: false }
      return { ...m, [n]: { ...cur, ...patch } }
    })

  const submit = async () => {
    if (!name.trim() || !prompt.trim() || tooMany || submitting) return
    setSubmitting(true)
    try {
      const data = {
        name: name.trim(),
        description: description.trim(),
        prompt,
        params,
      }
      const r = template
        ? await templatesApi.update(spaceId, template.id, data)
        : await templatesApi.create(spaceId, data)
      if (r.warnings.length > 0) {
        // Soft warnings: saved anyway, just notify (placeholder/param mismatches are usually typos).
        setWarnings(r.warnings)
        toast(`Saved, with ${r.warnings.length} placeholder warning${r.warnings.length === 1 ? '' : 's'}`, 'info')
      }
      onSaved()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/40"
      />
      <div className="msg-enter relative flex max-h-[85vh] w-full max-w-4xl flex-col rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]">
        <div className="mb-4 flex shrink-0 items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">
            {template ? 'Edit template' : 'New template'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_300px]">
            {/* Left column: basics + prompt */}
            <div>
              <label className="mb-1 block text-[12px] text-ink-2">Name</label>
              <input
                value={name}
                autoFocus
                maxLength={64}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Design review"
                className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
              />
              <label className="mb-1 block text-[12px] text-ink-2">
                Description<span className="text-ink-3"> (optional)</span>
              </label>
              <input
                value={description}
                maxLength={200}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What this template does"
                className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
              />
              <label className="mb-1 block text-[12px] text-ink-2">
                Instruction prompt
              </label>
              <PromptEditor value={prompt} onChange={setPrompt} />
            </div>
            {/* Right column: auto-extracted params */}
            <div>
              <label className="mb-1 block text-[12px] text-ink-2">
                Params
                <span className="text-ink-3"> (auto-extracted from prompt placeholders)</span>
              </label>
              {params.length === 0 ? (
                <p className="rounded-lg border border-dashed border-border px-3 py-4 text-[12px] leading-relaxed text-ink-3">
                  Write {'{param_name}'} in the prompt on the left and the param shows
                  up here. Templates without params skip the form and run on start.
                </p>
              ) : (
                <ul className="space-y-2">
                  {params.map((p) => (
                    <li
                      key={p.name}
                      className="msg-enter rounded-lg border border-border px-3 py-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 truncate font-mono text-[12px] text-ink">
                          {p.name}
                        </span>
                        <label className="flex shrink-0 items-center gap-1 text-[12px] text-ink-2">
                          <input
                            type="checkbox"
                            checked={p.required}
                            onChange={(e) =>
                              setMeta(p.name, { required: e.target.checked })
                            }
                          />
                          Required
                        </label>
                      </div>
                      <input
                        value={p.description}
                        maxLength={200}
                        onChange={(e) =>
                          setMeta(p.name, { description: e.target.value })
                        }
                        placeholder="Description (guides extraction on advance; be specific)"
                        className="mt-1.5 w-full rounded-md border border-border bg-bg px-2.5 py-1.5 text-[12px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
                      />
                    </li>
                  ))}
                </ul>
              )}
              {tooMany && (
                <p className="mt-2 text-[12px] text-danger">
                  At most {MAX_PARAMS} params — trim the placeholders in the prompt.
                </p>
              )}
            </div>
          </div>
          {warnings.length > 0 && (
            <ul className="mt-3 space-y-1 rounded-lg border border-border bg-surface-2 px-3 py-2">
              {warnings.map((w, i) => (
                <li key={i} className="text-[12px] text-ink-2">
                  ⚠ {w}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="mt-4 flex shrink-0 justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!name.trim() || !prompt.trim() || tooMany || submitting}
            className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------ Prompt editor
/**
 * Prompt editor with placeholder highlighting: a backdrop with transparent text
 * renders the highlight blocks, and the real textarea sits on top handling input
 * (font/padding aligned pixel by pixel). The textarea auto-grows with the content
 * (scrolling is left to the dialog body), so the backdrop height naturally matches.
 */
function PromptEditor({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const taRef = useRef<HTMLTextAreaElement>(null)
  const segments = useMemo(() => splitPrompt(value), [value])

  useEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight + 2}px` // +2 = top and bottom borders
  }, [value])

  return (
    <div>
      <div className="flex items-center justify-between gap-2 rounded-t-lg border border-b-0 border-border bg-surface-2 px-3 py-1.5">
        <p className="text-[11.5px] text-ink-3">
          Write{' '}
          <span className="rounded bg-accent-soft px-1 font-mono text-[11px] text-accent">
            {'{param_name}'}
          </span>{' '}
          to mark a param; the config appears on the right
        </p>
        <span className="shrink-0 font-mono text-[11px] text-ink-3">
          {value.length}/{PROMPT_MAX_LEN}
        </span>
      </div>
      <div className="relative">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words rounded-b-lg border border-transparent bg-bg px-3 py-2 font-mono text-[12.5px] leading-relaxed text-transparent"
        >
          {segments.map((s, i) =>
            s.kind === 'param' ? (
              <mark key={i} className="rounded bg-accent-soft text-transparent">
                {s.raw}
              </mark>
            ) : (
              s.text
            ),
          )}
          {/* Trailing placeholder: makes a final newline still occupy a row, matching the textarea height. */}
          {'\u200b'}
        </div>
        <textarea
          ref={taRef}
          value={value}
          rows={12}
          maxLength={PROMPT_MAX_LEN}
          onChange={(e) => onChange(e.target.value)}
          placeholder={'e.g. Based on {requirements_doc}, produce the design…'}
          className="relative block w-full resize-none overflow-hidden whitespace-pre-wrap break-words rounded-b-lg border border-border bg-transparent px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
        />
      </div>
    </div>
  )
}

// ------------------------------------------------------------ Workflow editor
function WorkflowEditor({
  spaceId,
  workflow,
  templates,
  onClose,
  onSaved,
}: {
  spaceId: string
  workflow: WorkflowTemplate | null
  templates: Template[]
  onClose: () => void
  onSaved: () => void
}) {
  const { toast } = useToast()
  const [name, setName] = useState(workflow?.name ?? '')
  const [description, setDescription] = useState(workflow?.description ?? '')
  const [nodes, setNodes] = useState<string[]>(
    workflow?.nodes.map((n) => n.template_id) ?? [templates[0]?.id ?? ''],
  )
  const [submitting, setSubmitting] = useState(false)

  const move = (i: number, dir: -1 | 1) =>
    setNodes((list) => {
      const j = i + dir
      if (j < 0 || j >= list.length) return list
      const next = [...list]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })

  const submit = async () => {
    if (!name.trim() || nodes.some((n) => !n) || submitting) return
    setSubmitting(true)
    try {
      const data = {
        name: name.trim(),
        description: description.trim(),
        nodes: nodes.map((template_id) => ({ template_id })),
      }
      if (workflow) await templatesApi.updateWorkflow(spaceId, workflow.id, data)
      else await templatesApi.createWorkflow(spaceId, data)
      onSaved()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-black/40"
      />
      <div className="msg-enter relative w-full max-w-lg rounded-xl border border-border bg-surface p-5 shadow-[var(--shadow)]">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">
            {workflow ? 'Edit workflow' : 'New workflow'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>
        <label className="mb-1 block text-[12px] text-ink-2">Name</label>
        <input
          value={name}
          autoFocus
          maxLength={64}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. End-to-end delivery"
          className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
        />
        <label className="mb-1 block text-[12px] text-ink-2">
          Description<span className="text-ink-3"> (optional)</span>
        </label>
        <input
          value={description}
          maxLength={200}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this workflow does"
          className="mb-3 w-full rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-ink-3 focus:border-border-strong focus:outline-none"
        />
        <div className="mb-1 flex items-center justify-between">
          <label className="block text-[12px] text-ink-2">
            Nodes (in run order; each node references a template)
          </label>
          <button
            type="button"
            onClick={() => setNodes((l) => [...l, templates[0]?.id ?? ''])}
            className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[12px] text-ink-2 hover:bg-surface-2 hover:text-ink"
          >
            <IconPlus className="h-3 w-3" />
            Add node
          </button>
        </div>
        <ul className="mb-4 space-y-2">
          {nodes.map((tid, i) => (
            <li key={i} className="flex items-center gap-2">
              <span className="w-6 shrink-0 text-center font-mono text-[12px] text-ink-3">
                {i + 1}
              </span>
              <select
                value={tid}
                onChange={(e) =>
                  setNodes((l) => l.map((v, j) => (j === i ? e.target.value : v)))
                }
                className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              >
                <option value="" disabled>
                  Select a template…
                </option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => move(i, -1)}
                disabled={i === 0}
                className="shrink-0 rounded-md px-1.5 py-1 text-[12px] text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-30"
              >
                ↑
              </button>
              <button
                type="button"
                onClick={() => move(i, 1)}
                disabled={i === nodes.length - 1}
                className="shrink-0 rounded-md px-1.5 py-1 text-[12px] text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-30"
              >
                ↓
              </button>
              <button
                type="button"
                onClick={() => setNodes((l) => l.filter((_, j) => j !== i))}
                disabled={nodes.length <= 1}
                title="Remove node"
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-danger-soft hover:text-danger disabled:opacity-30"
              >
                <IconTrash className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!name.trim() || nodes.some((n) => !n) || submitting}
            className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-medium text-accent-ink transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
