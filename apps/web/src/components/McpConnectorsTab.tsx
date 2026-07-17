import { useCallback, useEffect, useState } from 'react'
import { mcpApi } from '../api/endpoints'
import type { McpConnector, McpToolInfo } from '../api/types'
import { cn } from '../lib/cn'
import {
  isValidAlias,
  parseEnvLines,
  parseHeaderLines,
  splitArgs,
} from '../lib/mcp'
import { useToast } from '../state/toast'
import { IconChevron, IconEdit, IconPlus, IconTrash } from './icons'

/** Space MCP connectors: list / add / edit / delete / enable toggle + the
 * per-connector tool menu with an enabled-tool subset. Members can view;
 * only owners manage. Credential values are write-only — the backend echoes
 * header/env names, never values. */
export function McpConnectorsTab({
  spaceId,
  isOwner,
}: {
  spaceId: string
  isOwner: boolean
}) {
  const { toast } = useToast()
  const [servers, setServers] = useState<McpConnector[] | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<McpConnector | null>(null)

  const reload = useCallback(async () => {
    const r = await mcpApi.list(spaceId)
    setServers(r.servers)
  }, [spaceId])

  useEffect(() => {
    reload().catch((e) =>
      toast(e instanceof Error ? e.message : 'Failed to load connectors'),
    )
  }, [reload, toast])

  const toggleEnabled = async (server: McpConnector) => {
    try {
      await mcpApi.setEnabled(spaceId, server.alias, !server.enabled)
      await reload()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to update connector')
    }
  }

  const remove = async (server: McpConnector) => {
    try {
      await mcpApi.remove(spaceId, server.alias)
      await reload()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to delete connector')
    }
  }

  if (servers === null) {
    return <div className="h-24 animate-pulse rounded-lg bg-surface-2" />
  }

  return (
    <>
      <p className="mb-3 text-[11.5px] text-ink-3">
        MCP connectors give this space's sessions extra tools from Model
        Context Protocol servers. Credentials are stored server-side and never
        shown again after saving.
      </p>

      {servers.length === 0 && !formOpen && (
        <p className="mb-3 rounded-lg border border-border px-3 py-2 text-[12px] text-ink-3">
          No connectors configured yet.
        </p>
      )}

      <ul className="space-y-2">
        {servers.map((server) => (
          <ConnectorRow
            key={server.alias}
            spaceId={spaceId}
            server={server}
            isOwner={isOwner}
            onToggle={() => void toggleEnabled(server)}
            onEdit={() => {
              setEditing(server)
              setFormOpen(true)
            }}
            onDelete={() => void remove(server)}
          />
        ))}
      </ul>

      {isOwner && !formOpen && (
        <button
          type="button"
          onClick={() => {
            setEditing(null)
            setFormOpen(true)
          }}
          className="mt-3 flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] text-ink-2 hover:bg-surface-2"
        >
          <IconPlus className="h-3.5 w-3.5" />
          Add connector
        </button>
      )}

      {formOpen && (
        <ConnectorForm
          spaceId={spaceId}
          editing={editing}
          onClose={() => {
            setFormOpen(false)
            setEditing(null)
          }}
          onSaved={async () => {
            setFormOpen(false)
            setEditing(null)
            await reload()
          }}
        />
      )}
    </>
  )
}

// ------------------------------------------------------------ connector row

function ConnectorRow({
  spaceId,
  server,
  isOwner,
  onToggle,
  onEdit,
  onDelete,
}: {
  spaceId: string
  server: McpConnector
  isOwner: boolean
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  const [toolsOpen, setToolsOpen] = useState(false)
  const endpoint =
    server.type === 'http'
      ? server.url
      : [server.command, ...server.args].join(' ')
  const credentials =
    server.type === 'http' ? server.header_names : server.env_names

  return (
    <li className="rounded-lg border border-border">
      <div className="flex items-center gap-2.5 px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-ink">
              {server.alias}
            </span>
            <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10.5px] uppercase text-ink-3">
              {server.type}
            </span>
            {!server.enabled && (
              <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10.5px] text-ink-3">
                disabled
              </span>
            )}
          </div>
          <span className="block truncate text-[11.5px] text-ink-3" title={endpoint}>
            {endpoint}
          </span>
          {credentials.length > 0 && (
            <span className="block truncate text-[10.5px] text-ink-3">
              {server.type === 'http' ? 'Headers set: ' : 'Env set: '}
              {credentials.join(', ')}
            </span>
          )}
        </div>
        <label
          className="flex shrink-0 cursor-pointer items-center gap-1 text-[11.5px] text-ink-3"
          title="Enabled connectors are attached to this space's new sessions"
        >
          <input
            type="checkbox"
            checked={server.enabled}
            disabled={!isOwner}
            onChange={onToggle}
            className="h-3.5 w-3.5 accent-[var(--accent)]"
          />
          Enabled
        </label>
        {isOwner && (
          <>
            <button
              type="button"
              title="Edit connector"
              onClick={onEdit}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-ink"
            >
              <IconEdit className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              title="Delete connector"
              onClick={onDelete}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-danger"
            >
              <IconTrash className="h-3.5 w-3.5" />
            </button>
          </>
        )}
      </div>
      <button
        type="button"
        onClick={() => setToolsOpen((open) => !open)}
        className="flex w-full items-center gap-1 border-t border-border px-3 py-1.5 text-[11.5px] text-ink-3 hover:text-ink-2"
      >
        <IconChevron open={toolsOpen} className="h-3 w-3" />
        Tools
        {server.tools !== null && (
          <span className="text-ink-3">· {server.tools.length} enabled</span>
        )}
      </button>
      {toolsOpen && (
        <ToolSubsetEditor spaceId={spaceId} server={server} isOwner={isOwner} />
      )}
    </li>
  )
}

// -------------------------------------------------------- tool subset editor

function ToolSubsetEditor({
  spaceId,
  server,
  isOwner,
}: {
  spaceId: string
  server: McpConnector
  isOwner: boolean
}) {
  const { toast } = useToast()
  const [menu, setMenu] = useState<McpToolInfo[] | null>(null)
  const [menuError, setMenuError] = useState<string | null>(null)
  // null = all advertised tools enabled; a list = only those.
  const [subset, setSubset] = useState<string[] | null>(server.tools)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (server.type !== 'http') return
    mcpApi
      .toolMenu(spaceId, server.alias)
      .then((r) => setMenu(r.tools))
      .catch((e) =>
        setMenuError(
          e instanceof Error ? e.message : 'Failed to load the tool menu',
        ),
      )
  }, [spaceId, server.alias, server.type])

  const save = async () => {
    if (saving) return
    setSaving(true)
    try {
      await mcpApi.setTools(spaceId, server.alias, subset)
      toast('Tool selection saved', 'info')
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to save tool selection')
    } finally {
      setSaving(false)
    }
  }

  if (server.type !== 'http') {
    return (
      <p className="border-t border-border px-3 py-2 text-[11.5px] text-ink-3">
        Tool discovery is available for http connectors only. stdio connectors
        expose their full tool set when a session connects them.
      </p>
    )
  }
  if (menuError) {
    return (
      <p className="border-t border-border px-3 py-2 text-[11.5px] text-danger">
        {menuError}
      </p>
    )
  }
  if (menu === null) {
    return (
      <div className="border-t border-border px-3 py-2">
        <div className="h-10 animate-pulse rounded bg-surface-2" />
      </div>
    )
  }

  const allEnabled = subset === null

  return (
    <div className="space-y-1.5 border-t border-border px-3 py-2">
      <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink">
        <input
          type="checkbox"
          checked={allEnabled}
          disabled={!isOwner}
          onChange={(e) => setSubset(e.target.checked ? null : menu.map((t) => t.name))}
          className="h-3.5 w-3.5 accent-[var(--accent)]"
        />
        All tools
        <span className="text-[11px] text-ink-3">
          Newly advertised tools join automatically
        </span>
      </label>
      {!allEnabled && (
        <ul className="space-y-1">
          {menu.map((tool) => {
            const checked = subset?.includes(tool.name) ?? false
            return (
              <li key={tool.name} className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!isOwner}
                  onChange={(e) => {
                    const current = subset ?? []
                    setSubset(
                      e.target.checked
                        ? [...current, tool.name]
                        : current.filter((name) => name !== tool.name),
                    )
                  }}
                  className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--accent)]"
                />
                <span className="min-w-0">
                  <span className="block text-[12px] text-ink">{tool.name}</span>
                  {tool.description && (
                    <span className="block truncate text-[11px] text-ink-3">
                      {tool.description}
                    </span>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      {isOwner && (
        <div className="flex justify-end">
          <button
            type="button"
            disabled={saving}
            onClick={() => void save()}
            className="rounded-md bg-accent px-2.5 py-1 text-[11.5px] font-medium text-accent-ink disabled:opacity-40"
          >
            Save selection
          </button>
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------ add/edit form

function ConnectorForm({
  spaceId,
  editing,
  onClose,
  onSaved,
}: {
  spaceId: string
  editing: McpConnector | null
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { toast } = useToast()
  const [alias, setAlias] = useState(editing?.alias ?? '')
  const [type, setType] = useState<'http' | 'stdio'>(editing?.type ?? 'http')
  const [url, setUrl] = useState(editing?.url ?? '')
  const [headersText, setHeadersText] = useState('')
  const [command, setCommand] = useState(editing?.command ?? '')
  const [argsText, setArgsText] = useState(editing?.args.join(' ') ?? '')
  const [envText, setEnvText] = useState('')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    if (saving) return
    if (!isValidAlias(alias)) {
      toast('Alias must match ^[a-z0-9_-]{1,32}$')
      return
    }
    const headers = parseHeaderLines(headersText)
    if (!headers.ok) {
      toast(`Invalid header line: ${headers.badLine ?? ''} (use "Name: value")`)
      return
    }
    const env = parseEnvLines(envText)
    if (!env.ok) {
      toast(`Invalid env line: ${env.badLine ?? ''} (use "NAME=value")`)
      return
    }
    if (type === 'http' && !url.trim()) {
      toast('Endpoint URL is required for http connectors')
      return
    }
    if (type === 'stdio' && !command.trim()) {
      toast('Command is required for stdio connectors')
      return
    }
    setSaving(true)
    try {
      if (editing) {
        // Merge edit: send credentials only when the user typed replacements,
        // so the stored values survive untouched.
        await mcpApi.update(spaceId, editing.alias, {
          ...(type === 'http'
            ? {
                url: url.trim(),
                ...(headersText.trim() ? { headers: headers.value } : {}),
              }
            : {
                command: command.trim(),
                args: splitArgs(argsText),
                ...(envText.trim() ? { env: env.value } : {}),
              }),
        })
      } else {
        await mcpApi.create(spaceId, {
          alias,
          type,
          ...(type === 'http'
            ? { url: url.trim(), headers: headers.value }
            : {
                command: command.trim(),
                args: splitArgs(argsText),
                env: env.value,
              }),
        })
      }
      await onSaved()
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to save connector')
    } finally {
      setSaving(false)
    }
  }

  const storedCredentialNames =
    editing?.type === 'http' ? editing.header_names : (editing?.env_names ?? [])

  return (
    <div className="mt-3 space-y-2.5 rounded-lg border border-border p-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-[11.5px] text-ink-2">Alias</label>
          <input
            value={alias}
            disabled={editing !== null}
            maxLength={32}
            placeholder="github"
            onChange={(e) => setAlias(e.target.value)}
            className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink focus:border-border-strong focus:outline-none disabled:opacity-60"
          />
        </div>
        <div>
          <label className="mb-1 block text-[11.5px] text-ink-2">Transport</label>
          <select
            value={type}
            disabled={editing !== null}
            onChange={(e) => setType(e.target.value as 'http' | 'stdio')}
            className="w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-[12.5px] text-ink focus:outline-none disabled:opacity-60"
          >
            <option value="http">http (remote server)</option>
            <option value="stdio">stdio (local command)</option>
          </select>
        </div>
      </div>

      {type === 'http' ? (
        <>
          <div>
            <label className="mb-1 block text-[11.5px] text-ink-2">Endpoint URL</label>
            <input
              value={url}
              placeholder="https://mcp.example.com/mcp"
              onChange={(e) => setUrl(e.target.value)}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11.5px] text-ink-2">
              Headers (one "Name: value" per line)
              {editing && storedCredentialNames.length > 0 && (
                <span className="text-ink-3">
                  {' '}
                  — leave blank to keep the stored values (
                  {storedCredentialNames.join(', ')})
                </span>
              )}
            </label>
            <textarea
              value={headersText}
              rows={2}
              placeholder="Authorization: Bearer <token>"
              onChange={(e) => setHeadersText(e.target.value)}
              className="w-full resize-none rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[12px] text-ink focus:border-border-strong focus:outline-none"
            />
          </div>
        </>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-[11.5px] text-ink-2">Command</label>
              <input
                value={command}
                placeholder="npx"
                onChange={(e) => setCommand(e.target.value)}
                className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[12.5px] text-ink focus:border-border-strong focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-[11.5px] text-ink-2">
                Arguments (space-separated)
              </label>
              <input
                value={argsText}
                placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
                onChange={(e) => setArgsText(e.target.value)}
                className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[12px] text-ink focus:border-border-strong focus:outline-none"
              />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-[11.5px] text-ink-2">
              Environment (one "NAME=value" per line)
              {editing && storedCredentialNames.length > 0 && (
                <span className="text-ink-3">
                  {' '}
                  — leave blank to keep the stored values (
                  {storedCredentialNames.join(', ')})
                </span>
              )}
            </label>
            <textarea
              value={envText}
              rows={2}
              placeholder="API_TOKEN=<token>"
              onChange={(e) => setEnvText(e.target.value)}
              className="w-full resize-none rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[12px] text-ink focus:border-border-strong focus:outline-none"
            />
          </div>
        </>
      )}

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-border px-2.5 py-1.5 text-[12px] text-ink-2 hover:bg-surface-2"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={() => void save()}
          className={cn(
            'rounded-lg bg-accent px-3 py-1.5 text-[12px] font-medium text-accent-ink disabled:opacity-40',
          )}
        >
          {editing ? 'Save changes' : 'Add connector'}
        </button>
      </div>
    </div>
  )
}
