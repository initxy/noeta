import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Folder,
  ImagePlus,
  Layers,
  Plus,
  Settings,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  PromptInput,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  usePromptInputController,
} from "../components/ai-elements/prompt-input.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { ICON_LG, ICON_SM } from "../shared/icons.js";
import { classifyImageFile } from "./image-attach.js";
import {
  FALLBACK_PERMISSIONS,
  PERMISSION_META,
  permissionLabel,
  usePopoverDismiss,
} from "./chat-shared.js";

// U1 — landing-only affordance under the hero composer: a row of example-prompt
// chips that fill the composer on click. The current model / workspace / access
// mode already live in the composer's own chips, so no redundant defaults line or
// keyboard legend here. Purely additive; the hero composer is untouched above it.
const LANDING_EXAMPLES = [
  "Refactor a React component and add unit tests",
  "Run the test suite and fix the failures",
  "Open the README and summarize it",
  "Migrate a backend function into the SDK",
];

function LandingHints({ onFill }) {
  return (
    <div className="landing-hints">
      <div className="landing-examples">
        {LANDING_EXAMPLES.map((text) => (
          <button
            key={text}
            type="button"
            className="landing-example"
            onClick={() => onFill(text)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}
// the MCP control bar: a checkbox per configured server (its
// alias is what rides in the request body — never a url/token), an "add server"
// form that supports BOTH transports (http: alias + url + optional token;
// stdio: alias + command + args + env, configurable from the
// front-end given the personal-single-machine positioning), and a per-server
// "tools…" picker that lists the server's full tool menu and lets the user tick
// a subset (stored host-side, D6; the subset never rides a request body).
// the composer-side MCP bar is now JUST the per-session enable
// switches (a checkbox per configured server, alias-only — the session-level
// "which servers does this turn carry"). Adding / editing / deleting servers and
// picking each one's tool subset moved into the global ``McpServersPanel`` in the
// settings gear (reachable from there directly). The request body still carries
// only the enabled aliases (never a url / token, D3).
function McpBar({ servers, enabled, onToggle, onOpenPicker, disabled }) {
  const list = Array.isArray(servers) ? servers : [];
  return (
    <div className="mcp-bar" aria-label="MCP servers">
      {list.map((srv) => (
        <span className="mcp-chip" key={srv.alias}>
          <label title="Enable this server for the current turn · ⚙ pick its tool scope">
            <input
              type="checkbox"
              checked={enabled?.has?.(srv.alias) || false}
              disabled={disabled}
              onChange={() => onToggle?.(srv.alias)}
            />
            <span>{srv.alias}</span>
          </label>
          {/* U4 — jump straight to this server's tool-subset picker (opens the
              MCP panel's openPicker view for this alias), so "only give the model
              these tools" is one click from the enable chip, not 4 steps deep. */}
          <button
            type="button"
            className="mcp-chip__gear"
            title="Pick tool scope"
            aria-label={`Pick tool scope for ${srv.alias}`}
            onClick={() => onOpenPicker?.(srv)}
          >
            <Settings size={ICON_SM} />
          </button>
        </span>
      ))}
    </div>
  );
}

// the GLOBAL MCP server management panel (lives in the settings
// gear, parallel to Model / Permission / Workspace). Lists every
// configured server (credential-SCRUBBED — only ``header_names`` / ``env_names``
// ever come back) and offers, per server: Edit / Delete / Tools… (the tool-subset
// picker), plus a "+ New server" http/stdio form. Editing pre-fills url / command
// / args; the token / header / env value fields start EMPTY with a "leave blank =
// keep existing" hint — the PUT merge endpoint preserves any field not re-typed,
// so a secret is never displayed and never round-trips through the browser.
function McpServersPanel({
  servers,
  onCreate,
  onUpdate,
  onDelete,
  onDiscoverTools,
  onSetTools,
  autoPick,
}) {
  const list = Array.isArray(servers) ? servers : [];
  // ``editing`` is the alias being edited (null ⇒ none), ``adding`` the new-server
  // form toggle, ``picking`` the alias whose tool-subset picker is open.
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(null);
  const [picking, setPicking] = useState(null);
  const [menu, setMenu] = useState([]);
  const [ticks, setTicks] = useState(() => new Set());
  // Shared new/edit form fields.
  const [type, setType] = useState("http");
  const [alias, setAlias] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");

  const resetForm = () => {
    setType("http");
    setAlias("");
    setUrl("");
    setToken("");
    setCommand("");
    setArgsText("");
    setEnvText("");
    setAdding(false);
    setEditing(null);
  };
  const openAdd = () => {
    resetForm();
    setAdding(true);
  };
  const openEdit = (srv) => {
    setAdding(false);
    setPicking(null);
    setEditing(srv.alias);
    setType(srv.type === "stdio" ? "stdio" : "http");
    setAlias(srv.alias);
    setUrl(srv.url || "");
    setToken(""); // never pre-filled — leave blank = keep existing
    setCommand(srv.command || "");
    setArgsText(Array.isArray(srv.args) ? srv.args.join(" ") : "");
    setEnvText(""); // never pre-filled — leave blank = keep existing
  };

  const submitNew = async () => {
    const input =
      type === "stdio"
        ? {
            alias,
            type: "stdio",
            command,
            args: argsText.trim() ? argsText.trim().split(/\s+/) : [],
            env: parseEnvText(envText),
          }
        : {
            alias,
            type: "http",
            url,
            headers: token.trim()
              ? { Authorization: `Bearer ${token.trim()}` }
              : {},
          };
    const created = await onCreate?.(input);
    if (created) resetForm();
  };
  const submitEdit = async () => {
    // Only send the fields the user supplied — a blank token / env keeps the
    // host-side value (PUT merges; blank = keep existing).
    const input = {};
    if (type === "stdio") {
      input.command = command;
      input.args = argsText.trim() ? argsText.trim().split(/\s+/) : [];
      const env = parseEnvText(envText);
      if (Object.keys(env).length) input.env = env;
    } else {
      input.url = url;
      if (token.trim()) input.headers = { Authorization: `Bearer ${token.trim()}` };
    }
    const updated = await onUpdate?.(editing, input);
    if (updated) resetForm();
  };
  const remove = async (srv) => {
    const ok = await onDelete?.(srv.alias);
    if (ok && (editing === srv.alias || picking === srv.alias)) resetForm();
  };
  const openPicker = async (srv) => {
    setAdding(false);
    setEditing(null);
    const tools = await onDiscoverTools?.(srv.alias);
    if (!Array.isArray(tools)) return;
    setMenu(tools);
    const sub = Array.isArray(srv.tools) ? new Set(srv.tools) : null;
    setTicks(sub === null ? new Set(tools.map((t) => t.name)) : sub);
    setPicking(srv.alias);
  };
  const toggleTick = (name) =>
    setTicks((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  const savePicker = async () => {
    const all = menu.length > 0 && menu.every((t) => ticks.has(t.name));
    const subset = all ? null : Array.from(ticks);
    const saved = await onSetTools?.(picking, subset);
    if (saved) setPicking(null);
  };

  // U4 — when the composer's MCP chip gear requests this alias's picker, open it
  // directly. `autoPick` carries a bumping nonce so re-clicking the same gear
  // re-fires even when the alias is unchanged; the effect keys off the nonce.
  useEffect(() => {
    const alias = autoPick?.alias;
    if (!alias || !autoPick?.nonce) return;
    const srv = list.find((s) => s.alias === alias);
    if (srv) openPicker(srv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPick?.nonce]);

  const isEditing = editing !== null;
  return (
    <div className="mcp-panel" role="menu" aria-label="MCP servers">
      <div className="menu-section-label">MCP Servers</div>
      <div className="mcp-panel__list">
        {list.length === 0 ? (
          <EmptyState kind="mcp" title="No MCP servers configured yet" />
        ) : (
          list.map((srv) => (
            <div className="mcp-panel__row" key={srv.alias}>
              <span
                className="mcp-panel__alias"
                title={srv.type === "stdio" ? srv.command : srv.url}
              >
                {srv.alias}
                <span className="mcp-panel__type">{srv.type}</span>
              </span>
              <div className="mcp-panel__row-actions">
                <button type="button" onClick={() => openEdit(srv)}>
                  Edit
                </button>
                <button type="button" onClick={() => openPicker(srv)}>
                  Tools…
                </button>
                <button
                  type="button"
                  className="mcp-panel__delete"
                  onClick={() => remove(srv)}
                >
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </div>
      {picking ? (
        <div className="mcp-tools-form" aria-label="MCP tool subset">
          {menu.length === 0 ? (
            <span>(no tools advertised)</span>
          ) : (
            menu.map((t) => (
              <label className="mcp-tool-tick" key={t.name} title={t.description}>
                <input
                  type="checkbox"
                  checked={ticks.has(t.name)}
                  onChange={() => toggleTick(t.name)}
                />
                <span>{t.name}</span>
              </label>
            ))
          )}
          <div className="mcp-panel__form-actions">
            <button type="button" onClick={() => setPicking(null)}>
              Cancel
            </button>
            <button type="button" onClick={savePicker}>
              Save tools
            </button>
          </div>
        </div>
      ) : null}
      {adding || isEditing ? (
        <div className="mcp-add-form" aria-label={isEditing ? "Edit MCP server" : "New MCP server"}>
          <select
            value={type}
            disabled={isEditing}
            onChange={(e) => setType(e.target.value)}
          >
            <option value="http">http</option>
            <option value="stdio">stdio</option>
          </select>
          <input
            type="text"
            placeholder="alias"
            value={alias}
            disabled={isEditing}
            onChange={(e) => setAlias(e.target.value)}
          />
          {type === "stdio" ? (
            <>
              <input
                type="text"
                placeholder="command (e.g. npx)"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
              />
              <input
                type="text"
                placeholder="args (space-separated)"
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
              />
              <input
                type="text"
                placeholder={isEditing ? "env KEY=VAL (blank = keep existing)" : "env KEY=VAL space-separated"}
                value={envText}
                onChange={(e) => setEnvText(e.target.value)}
              />
            </>
          ) : (
            <>
              <input
                type="text"
                placeholder="https://…/mcp"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <input
                type="password"
                placeholder={isEditing ? "token (blank = keep existing)" : "token (optional)"}
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </>
          )}
          <div className="mcp-panel__form-actions">
            <button type="button" onClick={resetForm}>
              Cancel
            </button>
            <button type="button" onClick={isEditing ? submitEdit : submitNew}>
              {isEditing ? "Save changes" : "Add server"}
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="mcp-add-btn" onClick={openAdd}>
          <Plus size={ICON_SM} /> New server
        </button>
      )}
    </div>
  );
}

// Parse a "KEY=VAL KEY2=VAL2" string into an env object (skips malformed pairs).
function parseEnvText(raw) {
  const out = {};
  for (const pair of String(raw || "").trim().split(/\s+/)) {
    if (!pair) continue;
    const eq = pair.indexOf("=");
    if (eq <= 0) continue;
    out[pair.slice(0, eq)] = pair.slice(eq + 1);
  }
  return out;
}

function ChatComposer({
  autoFocus,
  canSend,
  commandIn,
  isNewSession,
  onPermissionChange,
  onModelChange,
  onEffortChange,
  onWorkspaceChange,
  onSubmit,
  onCreateMcpServer,
  onUpdateMcpServer,
  onDeleteMcpServer,
  onDiscoverMcpTools,
  onSetMcpTools,
  mcpServers,
  enabledMcp,
  onToggleMcp,
  options,
  permission,
  model,
  effort,
  selectedWorkspace,
  boundWorkspaceName,
  onNotice,
  working,
  onStop,
}) {
  // Auto-focus the composer the moment the agent finishes a turn (canSend flips
  // false → true while a session is open), so the user can type the next message
  // without reaching for the box. The landing case is handled by autoFocus.
  const textareaRef = useRef(null);
  const wasSendable = useRef(canSend);
  useEffect(() => {
    if (canSend && !wasSendable.current && !isNewSession) {
      textareaRef.current?.focus();
    }
    wasSendable.current = canSend;
  }, [canSend, isNewSession]);

  const [slashSelection, setSlashSelection] = useState(0);
  const [slashMenuPosition, setSlashMenuPosition] = useState(null);
  // U4 — a gear click on an MCP enable chip records {alias, nonce}; the bumping
  // nonce lets the "+" add-menu / MCP panel re-open the same alias's picker on a
  // repeat click. The MCP panel management surface stays inside the add-menu.
  const [mcpPicker, setMcpPicker] = useState({ alias: null, nonce: 0 });
  // Slash-menu skills come straight from /capabilities; the thin backend feeds
  // an empty list (the menu degrades gracefully, like the empty workspaces /
  // providers surfaces) so there is nothing to discover per-workspace.
  const workspaceSkills = Array.isArray(options.skills) ? options.skills : [];

  // The composer's backend-backed selectors (codex addendum): permission mode,
  // plus the re-surfaced model / effort (per-turn) and workspace (create-once).
  // Each menu comes from the live /capabilities list; permission keeps a static
  // fallback until caps resolve. The chosen value is always kept present so it
  // never vanishes from its own menu.
  const permissionItems = pickItems(
    options.permissionModes,
    FALLBACK_PERMISSIONS,
    permission,
  ).map((name) => ({
    value: name,
    label: permissionLabel(name),
    hint: PERMISSION_META[name]?.hint,
  }));
  const modelItems = (Array.isArray(options.models) ? options.models : []).map(
    (name) => ({ value: name, label: name }),
  );
  const effortItems = (
    Array.isArray(options.effortModes) ? options.effortModes : []
  ).map((name) => ({ value: name, label: name }));
  const workspaceItems = (
    Array.isArray(options.workspaces) ? options.workspaces : []
  ).map((ws) => ({ value: ws.id, label: ws.name || ws.path || ws.id }));

  // T4 / spec D7: images pasted or picked for THIS turn. Each entry is
  // {id, media_type, data_base64, dataUrl}: the first two ride the request body
  // (base64 ONLY on the wire — the ledger stores a ContentRef, not base64), the
  // dataUrl is a local-only handle for the thumbnail chip. Cleared on submit.
  const [pastedImages, setPastedImages] = useState([]);
  const removePastedImage = (id) =>
    setPastedImages((prev) => prev.filter((img) => img.id !== id));

  // T4 / spec D5 vision gate: can the current (bound) model read images?
  // Capability comes from /capabilities' model_capabilities map. Only gate when
  // the model is registered AND explicitly supports_vision === false; unknown /
  // missing models (no bound model yet on a fresh session, an old server lacking
  // the field) fail open — if an image is sent to a non-vision model the backend
  // Responses guard still backstops it. Gate only when we KNOW it's unsupported.
  const effectiveModel = model;
  const visionCap = options.modelCapabilities?.[effectiveModel];
  const modelSupportsVision = !visionCap || visionCap.supports_vision !== false;
  // The paste handler is a useCallback defined before the gate is computed, so a
  // ref lets it read the latest value each render without a TDZ in its deps.
  const supportsVisionRef = useRef(true);
  useEffect(() => {
    supportsVisionRef.current = modelSupportsVision;
  }, [modelSupportsVision]);

  // T4 / spec D7: read every image file off a paste OR the file picker, validate
  // against the media-type whitelist + 5MB cap (the pure, unit-tested
  // ``classifyImageFile``), base64-encode it, and queue it as a chip. Takes any
  // iterable of File objects, so paste and the <input type=file> picker share ONE
  // ingestion path. Non-image pastes fall through untouched; a rejected type /
  // oversize image surfaces a notice rather than silently dropping.
  const ingestImageFiles = useCallback((files) => {
    for (const file of files) {
      const verdict = classifyImageFile(file);
      if (!verdict.ok) {
        if (verdict.reason !== "missing") onNotice?.("error", verdict.message);
        continue;
      }
      const mediaType = verdict.mediaType;
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        // data:<media_type>;base64,<payload> → strip the prefix to the payload.
        const comma = result.indexOf(",");
        const dataBase64 = comma >= 0 ? result.slice(comma + 1) : "";
        if (!dataBase64) return;
        setPastedImages((prev) => [
          ...prev,
          {
            id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
            media_type: mediaType,
            data_base64: dataBase64,
            dataUrl: result,
          },
        ]);
      };
      // A failed/aborted read must not silently drop the image — surface a notice
      // like the validation path, so the user doesn't send a turn believing the
      // image was attached.
      reader.onerror = () => {
        onNotice?.("error", `Failed to read image "${file.name || "image"}".`);
      };
      reader.readAsDataURL(file);
    }
  }, [onNotice]);

  const handleComposerPaste = useCallback(
    (event) => {
      const items = event.clipboardData?.items;
      if (!items || !items.length) return;
      const files = [];
      for (const item of items) {
        if (item.kind === "file" && String(item.type || "").startsWith("image/")) {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }
      if (!files.length) return;
      // An image was on the clipboard — intercept so it doesn't paste as text /
      // a bogus filename, and queue the image chips instead.
      event.preventDefault();
      // Vision gate: when the model can't read images, swallow the paste (no
      // chip) and give one light hint instead of firing a doomed turn.
      if (!supportsVisionRef.current) {
        onNotice?.("info", "This model can't read images; switch to a vision-capable model.");
        return;
      }
      ingestImageFiles(files);
    },
    [ingestImageFiles, onNotice],
  );

  // T4 / spec D7 — the second image-input entry point: a button that opens the
  // system file picker (hidden <input type=file accept=image/* multiple>). The
  // chosen files feed the SAME ``ingestImageFiles`` as paste. Reset ``value``
  // after each pick so picking the same file twice still fires a change event.
  const fileInputRef = useRef(null);
  const onPickFiles = (event) => {
    const files = event.target.files;
    if (files && files.length) ingestImageFiles(files);
    event.target.value = "";
  };

  return (
    <div className="composer-block">
    <PromptInput
      className="composer"
      onSubmit={({ text }) => {
        // Built-in slash commands are host-owned. The composer menu only
        // inserts their text; submitting sends the slash form through the same
        // command-in path as any other goal.
        // T4 / spec D7: ship the queued images alongside the goal, then clear
        // them so the next turn starts fresh. The images carry only
        // {media_type, data_base64} on the wire (the local dataUrl thumbnail
        // handle stays in the browser).
        const queuedImages = pastedImages.map((img) => ({
          media_type: img.media_type,
          data_base64: img.data_base64,
        }));
        setPastedImages([]);
        return onSubmit(text, queuedImages);
      }}
    >
      <SlashMenu
        slashCommands={options.slashCommands}
        skills={workspaceSkills}
        position={slashMenuPosition}
        selectedIndex={slashSelection}
        onSelectionChange={setSlashSelection}
      />
      {/* U2 — "attachments & MCP" bar ABOVE the textarea: pasted-image chips and
          per-turn MCP enable chips share one horizontally-scrolling row so they
          never crowd the bottom action bar (which now holds only functional chips
          + the pinned submit). */}
      <div className="composer-attach-bar">
        {pastedImages.length ? (
          <div className="image-chips" aria-label="pasted images">
            {pastedImages.map((img) => (
              <span className="image-chip" key={img.id}>
                <img
                  className="image-chip__thumb"
                  src={img.dataUrl}
                  alt="pasted image"
                />
                <button
                  type="button"
                  aria-label="remove image"
                  onClick={() => removePastedImage(img.id)}
                >
                  <X size={ICON_SM} />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <McpBar
          servers={mcpServers}
          enabled={enabledMcp}
          onToggle={onToggleMcp}
          onOpenPicker={(srv) =>
            setMcpPicker((prev) => ({ alias: srv.alias, nonce: prev.nonce + 1 }))
          }
          disabled={!commandIn}
        />
      </div>
      <SlashAwareComposerTextarea
        autoFocus={autoFocus}
        canSend={canSend}
        inputRef={textareaRef}
        onPaste={handleComposerPaste}
        selectedIndex={slashSelection}
        setPosition={setSlashMenuPosition}
        setSelectedIndex={setSlashSelection}
        slashCommands={options.slashCommands}
        skills={workspaceSkills}
      />
      {/* T4 / spec D7 — hidden image picker: opens the system file picker
          (multi-select, images only) when the "+" add-menu fires onPickImage.
          Chosen files feed straight into ingestImageFiles, shared with paste. */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        multiple
        className="composer-file-input"
        tabIndex={-1}
        aria-hidden="true"
        onChange={onPickFiles}
      />
      <PromptInputFooter>
        {/* codex bottom bar — left cluster: the "+" add-menu (images / files +
            MCP server management), then the inline access (permission) chip. */}
        <div className="composer-bar__left">
          <AddMenu
            disabled={!commandIn}
            canAttachImage={canSend && modelSupportsVision}
            visionBlocked={!modelSupportsVision}
            onPickImage={() => fileInputRef.current?.click()}
            mcpServers={mcpServers}
            onCreateMcpServer={onCreateMcpServer}
            onUpdateMcpServer={onUpdateMcpServer}
            onDeleteMcpServer={onDeleteMcpServer}
            onDiscoverMcpTools={onDiscoverMcpTools}
            onSetMcpTools={onSetMcpTools}
            openPickerReq={mcpPicker}
          />
          {/* codex working-folder chip: an interactive project picker on a new
              session, the read-only durable binding on an existing one. Lives
              inline in the bottom bar (not below the box) so it reads as one more
              turn-context control next to access / model. */}
          <ComposerWorkspace
            disabled={!commandIn}
            isNewSession={isNewSession}
            selectedWorkspace={selectedWorkspace}
            workspaceItems={workspaceItems}
            boundWorkspaceName={boundWorkspaceName}
            onWorkspaceChange={onWorkspaceChange}
          />
          {permissionItems.length ? (
            <AccessChip
              disabled={!commandIn}
              permission={permission}
              items={permissionItems}
              onChange={onPermissionChange}
            />
          ) : null}
          {/* U2 — model · effort chip moved into the left cluster so the ONLY
              thing in the right cluster is submit, which therefore can never be
              pushed off by a long model name / extra chip. */}
          {modelItems.length || effortItems.length ? (
            <ModelEffortChip
              disabled={!commandIn}
              model={model}
              effort={effort}
              modelItems={modelItems}
              effortItems={effortItems}
              onModelChange={onModelChange}
              onEffortChange={onEffortChange}
            />
          ) : null}
        </div>
        {/* right cluster: submit only — pinned, always visible. */}
        <div className="composer-bar__right">
          <PromptInputSubmit
            status={working ? "streaming" : "ready"}
            onStop={onStop}
            stopTooltip="Stop the current response"
            disabled={!canSend && !working}
            tabIndex={-1}
          />
        </div>
      </PromptInputFooter>
    </PromptInput>
    </div>
  );
}
// codex "+" add-menu (bottom-bar left): attach images/files (shares the paste
// ingestion path) and, one level in, the global MCP server management panel
// (formerly buried in the settings gear). Degrades gracefully — the MCP entry
// is always present; the image entry disables when the model can't read images.
function AddMenu({
  disabled,
  canAttachImage,
  visionBlocked,
  onPickImage,
  mcpServers,
  onCreateMcpServer,
  onUpdateMcpServer,
  onDeleteMcpServer,
  onDiscoverMcpTools,
  onSetMcpTools,
  openPickerReq,
}) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState("menu"); // "menu" | "mcp"
  const rootRef = useRef(null);
  const close = useCallback(() => {
    setOpen(false);
    setView("menu");
  }, []);
  usePopoverDismiss(open, close, rootRef);

  // U4 — a gear click on an MCP enable chip pops this menu open on the MCP view
  // (the tool-subset picker for that alias follows via autoPick below). Keyed on
  // the request nonce so each click re-opens.
  useEffect(() => {
    if (openPickerReq?.alias && openPickerReq.nonce) {
      setOpen(true);
      setView("mcp");
    }
  }, [openPickerReq?.nonce]);
  return (
    <div className="composer-chip-root composer-add" ref={rootRef}>
      <button
        type="button"
        className={`composer-add__btn${open ? " is-open" : ""}`}
        disabled={disabled}
        tabIndex={-1}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Add"
        title="Add"
        onClick={() => {
          if (!disabled) setOpen((current) => !current);
        }}
      >
        <Plus size={18} />
      </button>
      {open ? (
        <div
          className={`composer-pop__menu composer-pop__menu--left add-menu${
            view === "mcp" ? " add-menu--wide" : ""
          }`}
          role="menu"
        >
          {view === "menu" ? (
            <div className="menu-option-list">
              <button
                type="button"
                className="menu-option"
                disabled={!canAttachImage}
                title={
                  visionBlocked
                    ? "This model can't read images; switch to a vision-capable model"
                    : undefined
                }
                onClick={() => {
                  onPickImage?.();
                  close();
                }}
              >
                <span className="menu-option__lead">
                  <ImagePlus size={ICON_LG} /> Add photos or files
                </span>
              </button>
              <button
                type="button"
                className="menu-option"
                onClick={() => setView("mcp")}
              >
                <span className="menu-option__lead">
                  <Layers size={ICON_LG} /> MCP servers
                </span>
                <ChevronRight size={ICON_SM} />
              </button>
            </div>
          ) : (
            <div className="add-menu__mcp">
              <button
                type="button"
                className="add-menu__back"
                onClick={() => setView("menu")}
              >
                <ChevronRight size={ICON_SM} className="add-menu__back-icon" /> Back
              </button>
              <McpServersPanel
                servers={mcpServers}
                onCreate={onCreateMcpServer}
                onUpdate={onUpdateMcpServer}
                onDelete={onDeleteMcpServer}
                onDiscoverTools={onDiscoverMcpTools}
                onSetTools={onSetMcpTools}
                autoPick={openPickerReq}
              />
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// codex access chip (bottom-bar left): the live permission mode, an inline
// dropdown. The risky "bypassPermissions" mode flips the chip to the amber
// danger styling with a warning glyph (codex's "full access" slot).
function AccessChip({ disabled, permission, items, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  usePopoverDismiss(open, close, rootRef);
  const danger = permission === "bypassPermissions";
  return (
    <div className="composer-chip-root" ref={rootRef}>
      <button
        type="button"
        className={`composer-chip composer-chip--access${
          danger ? " composer-chip--danger" : ""
        }${open ? " is-open" : ""}`}
        disabled={disabled}
        tabIndex={-1}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Access mode: ${permissionLabel(permission)}`}
        title={PERMISSION_META[permission]?.hint || "Access mode"}
        onClick={() => {
          if (!disabled) setOpen((current) => !current);
        }}
      >
        {danger ? <AlertTriangle size={ICON_SM} /> : <ShieldCheck size={ICON_SM} />}
        <span className="composer-chip__label">
          {danger ? `⚠️ ${permissionLabel(permission)}` : permissionLabel(permission)}
        </span>
        <ChevronDown size={ICON_SM} className="composer-chip__caret" />
      </button>
      {open ? (
        <div className="composer-pop__menu composer-pop__menu--left" role="menu">
          <MenuOptionList
            className="permission-options"
            currentValue={permission}
            items={items}
            label="Access"
            onPick={(next) => {
              onChange?.(next);
              close();
            }}
          />
        </div>
      ) : null}
    </div>
  );
}

// codex model · effort chip (bottom-bar right): shows "model effort" and opens a
// combined picker. Picks DON'T close the menu, so model then effort can be set
// in one open (the chip label tracks the choice live); click-away / Esc closes.
function ModelEffortChip({
  disabled,
  model,
  effort,
  modelItems,
  effortItems,
  onModelChange,
  onEffortChange,
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  usePopoverDismiss(open, close, rootRef);
  const label = [model, effort].filter(Boolean).join(" ") || "Model";
  return (
    <div className="composer-chip-root" ref={rootRef}>
      <button
        type="button"
        className={`composer-chip composer-chip--model${open ? " is-open" : ""}`}
        disabled={disabled}
        tabIndex={-1}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Model and effort: ${label}`}
        title="Model and reasoning effort"
        onClick={() => {
          if (!disabled) setOpen((current) => !current);
        }}
      >
        <span className="composer-chip__label">{label}</span>
        <ChevronDown size={ICON_SM} className="composer-chip__caret" />
      </button>
      {open ? (
        <div
          className="composer-pop__menu composer-pop__menu--right model-effort-menu"
          role="menu"
        >
          {modelItems.length ? (
            <MenuOptionList
              className="model-options"
              currentValue={model}
              items={modelItems}
              label="Model"
              onPick={(next) => onModelChange?.(next)}
            />
          ) : null}
          {effortItems.length ? (
            <MenuOptionList
              className="effort-options"
              currentValue={effort}
              items={effortItems}
              label="Effort"
              onPick={(next) => onEffortChange?.(next)}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// codex working-folder chip (bottom-bar left). New session: an interactive project
// picker (the create-once workspace binding). Existing session: a read-only
// folder badge of the durable binding. Hidden entirely when no projects exist
// and nothing is bound (thin backend), matching the old graceful degradation.
function ComposerWorkspace({
  disabled,
  isNewSession,
  selectedWorkspace,
  workspaceItems,
  boundWorkspaceName,
  onWorkspaceChange,
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  usePopoverDismiss(open, close, rootRef);

  if (!isNewSession) {
    return (
      <div
        className="composer-chip composer-chip--static"
        title="Workspace is fixed once a session starts"
      >
        <Folder size={ICON_SM} />
        <span className="composer-chip__label">
          {boundWorkspaceName || "Default workspace"}
        </span>
      </div>
    );
  }
  if (!workspaceItems.length) return null;
  const current = workspaceItems.find((item) => item.value === selectedWorkspace);
  const label = current?.label || "Select project";
  return (
    <div className="composer-chip-root" ref={rootRef}>
      <button
        type="button"
        className={`composer-chip composer-chip--workspace${open ? " is-open" : ""}`}
        disabled={disabled}
        tabIndex={-1}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Working folder"
        onClick={() => {
          if (!disabled) setOpen((current2) => !current2);
        }}
      >
        <Folder size={ICON_SM} />
        <span className="composer-chip__label">{label}</span>
        <ChevronDown size={ICON_SM} className="composer-chip__caret" />
      </button>
      {open ? (
        <div className="composer-pop__menu composer-pop__menu--left" role="menu">
          <MenuOptionList
            className="workspace-menu"
            currentValue={selectedWorkspace}
            items={workspaceItems}
            label="Workspace"
            onPick={(next) => {
              onWorkspaceChange?.(next);
              close();
            }}
          />
        </div>
      ) : null}
    </div>
  );
}

function MenuOptionList({
  className = "",
  currentValue,
  disabled = false,
  items,
  label,
  onPick,
}) {
  return (
    <div className={`menu-options ${className}`}>
      <div className="menu-section-label">{label}</div>
      <div className="menu-option-list">
        {items.map((item) => (
          <button
            aria-pressed={item.value === currentValue}
            className={`menu-option ${item.value === currentValue ? "is-selected" : ""}`}
            disabled={disabled}
            key={item.value || item.label}
            type="button"
            title={item.hint || undefined}
            onClick={() => onPick(item.value)}
          >
            <span className="menu-option__body">
              <span className="menu-option__label">{item.label}</span>
              {item.hint ? (
                <span className="menu-option__hint">{item.hint}</span>
              ) : null}
            </span>
            {item.value === currentValue ? <Check size={ICON_SM} /> : null}
          </button>
        ))}
      </div>
    </div>
  );
}

// Prefer the live list; fall back to a static one when it is empty; always keep
// the currently-selected value present so it stays visible in its own menu.
function pickItems(live, fallback, current) {
  const base = Array.isArray(live) && live.length ? live : fallback;
  if (current && !base.includes(current)) return [current, ...base];
  return base;
}

function SlashAwareComposerTextarea({
  autoFocus,
  canSend,
  inputRef,
  onPaste,
  selectedIndex,
  setPosition,
  setSelectedIndex,
  slashCommands,
  skills,
}) {
  const controller = usePromptInputController();
  return (
    <PromptInputTextarea
      autoFocus={autoFocus}
      disabled={!canSend}
      inputRef={inputRef}
      onPaste={onPaste}
      onChange={(event) => {
        setPosition(getTextareaCaretPosition(event.currentTarget));
        setSelectedIndex(0);
      }}
      onClick={(event) => setPosition(getTextareaCaretPosition(event.currentTarget))}
      onKeyUp={(event) => setPosition(getTextareaCaretPosition(event.currentTarget))}
      onKeyDown={(event) => {
        const handled = handleSlashMenuKeyDown({
          controller,
          event,
          controllerText: controller.textInput.value,
          selectedIndex,
          setPosition,
          setSelectedIndex,
          slashCommands,
          skills,
        });
        if (handled) return;
      }}
      placeholder="Message the agent, or type / for commands"
    />
  );
}

const SLASH_MENU_LIMIT = 10;

function slashMenuItems({ text, slashCommands = [], skills = [] }) {
  if (!text.startsWith("/") || /\s/.test(text)) return [];
  const query = text.slice(1).toLowerCase();
  const commands = (slashCommands || [])
    .filter((command) => {
      const name = String(command?.name || "").toLowerCase();
      const description = String(command?.description || "").toLowerCase();
      return name.includes(query) || description.includes(query);
    })
    .map((command) => ({ kind: "command", key: `command:${command.name}`, command }));
  const skillItems = (skills || [])
    .filter((skill) => {
      const name = String(skill?.name || "").toLowerCase();
      const description = String(skill?.description || "").toLowerCase();
      return name.includes(query) || description.includes(query);
    })
    .map((skill) => ({ kind: "skill", key: `skill:${skill.name}`, skill }));
  return [...commands, ...skillItems].slice(0, SLASH_MENU_LIMIT);
}

function applySlashMenuItem(controller, item) {
  if (!item) return;
  if (item.kind === "command") {
    controller.textInput.setInput(`/${item.command.name} `);
  } else if (item.kind === "skill") {
    controller.textInput.setInput(`/${item.skill.name} `);
  }
  requestAnimationFrame(() => {
    const textarea = document.querySelector(".composer .ai-prompt-textarea");
    textarea?.focus();
    const valueLength = textarea?.value.length || 0;
    textarea?.setSelectionRange(valueLength, valueLength);
  });
}

function handleSlashMenuKeyDown({
  controller,
  event,
  controllerText,
  selectedIndex,
  setPosition,
  setSelectedIndex,
  slashCommands,
  skills,
}) {
  if (event.nativeEvent.isComposing) return false;
  const items = slashMenuItems({
    text: controllerText,
    slashCommands,
    skills,
  });
  if (!items.length) return false;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setSelectedIndex((current) => (current + 1) % items.length);
    return true;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    setSelectedIndex((current) => (current - 1 + items.length) % items.length);
    return true;
  }
  if (event.key === "Tab") {
    event.preventDefault();
    applySlashMenuItem(controller, items[selectedIndex] || items[0]);
    setPosition(null);
    return true;
  }
  return false;
}

function getTextareaCaretPosition(textarea) {
  if (!textarea || !textarea.value.startsWith("/") || /\s/.test(textarea.value)) return null;
  const form = textarea.closest("form");
  if (!form) return null;

  const textareaStyle = getComputedStyle(textarea);
  const mirror = document.createElement("div");
  const properties = [
    "boxSizing",
    "width",
    "paddingTop",
    "paddingRight",
    "paddingBottom",
    "paddingLeft",
    "borderTopWidth",
    "borderRightWidth",
    "borderBottomWidth",
    "borderLeftWidth",
    "fontFamily",
    "fontSize",
    "fontWeight",
    "fontStyle",
    "letterSpacing",
    "lineHeight",
    "textTransform",
    "textIndent",
    "textAlign",
    "wordSpacing",
    "tabSize",
  ];
  for (const property of properties) mirror.style[property] = textareaStyle[property];
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";

  const beforeCaret = textarea.value.slice(0, textarea.selectionStart ?? textarea.value.length);
  const marker = document.createElement("span");
  marker.textContent = "\u200b";
  mirror.textContent = beforeCaret;
  mirror.append(marker);
  document.body.append(mirror);

  const formRect = form.getBoundingClientRect();
  const textareaRect = textarea.getBoundingClientRect();
  const markerRect = marker.getBoundingClientRect();
  const mirrorRect = mirror.getBoundingClientRect();
  const lineHeight = Number.parseFloat(textareaStyle.lineHeight) || 20;
  const left = textareaRect.left - formRect.left + markerRect.left - mirrorRect.left - textarea.scrollLeft;
  const top = textareaRect.top - formRect.top + markerRect.top - mirrorRect.top - textarea.scrollTop + lineHeight;
  mirror.remove();

  return {
    left: Math.max(8, Math.min(left, form.clientWidth - 440)),
    top: Math.max(8, top),
  };
}

function SlashMenu({
  slashCommands = [],
  skills = [],
  position,
  selectedIndex,
  onSelectionChange,
}) {
  const controller = usePromptInputController();
  const text = controller.textInput.value;
  const items = useMemo(
    () => slashMenuItems({ text, slashCommands, skills }),
    [text, slashCommands, skills],
  );
  useEffect(() => {
    if (selectedIndex >= items.length) onSelectionChange(0);
  }, [items.length, onSelectionChange, selectedIndex]);
  if (!items.length) return null;
  const style = position
    ? { left: `${position.left}px`, top: `${position.top}px` }
    : undefined;
  return (
    <div className="slash-menu" style={style} role="listbox">
      {items.map((item, index) => {
        const selected = index === selectedIndex;
        if (item.kind === "command") {
          const command = item.command;
          return (
            <button
              className={`slash-option${selected ? " is-selected" : ""}`}
              key={item.key}
              type="button"
              role="option"
              aria-selected={selected}
              onMouseEnter={() => onSelectionChange(index)}
              onClick={() => applySlashMenuItem(controller, item)}
            >
              <span className="slash-command">/{command.name}</span>
              <span className="slash-label">
                <span className="slash-title">
                  {command.argument_hint ? `/${command.name} ${command.argument_hint}` : `/${command.name}`}
                </span>
                <span className="slash-desc">{command.description}</span>
              </span>
            </button>
          );
        }
        if (item.kind === "skill") {
          const skill = item.skill;
          return (
            <button
              className={`slash-option slash-option--skill${selected ? " is-selected" : ""}`}
              key={item.key}
              type="button"
              role="option"
              aria-selected={selected}
              onMouseEnter={() => onSelectionChange(index)}
              onClick={() => applySlashMenuItem(controller, item)}
            >
              <span className="slash-command">/{skill.name}</span>
              <span className="slash-label">
                <span className="slash-title">
                  {skill.argument_hint ? `/${skill.name} ${skill.argument_hint}` : `/${skill.name}`}
                </span>
                <span className="slash-desc">{skill.description}</span>
              </span>
            </button>
          );
        }
        return null;
      })}
    </div>
  );
}

export { ChatComposer, LandingHints };
