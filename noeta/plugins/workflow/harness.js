// Runs one Workflow script as far as the agent results known so far allow.
//
// Input (stdin, JSON): {mode: "check" | "run", script, args, results: {key: value},
// failed: [key], spent, maxItems}. A script is a meta literal and a body. In
// "check" mode only the meta is read. In "run" mode the body runs in a fresh
// V8 context with no Node.js API: agent() returns the recorded result of a
// call it has seen before (keyed by its prompt, options and occurrence) and
// never settles otherwise; once nothing else can run, the calls still waiting
// are reported and the process exits. Replaying the same script with more
// results moves it further, so a run survives restarts: only finished agent
// results are kept, never this process.
//
// Output (stdout, JSON): {meta} | {error} | {status: "done", value, logs} |
// {status: "error", error, logs} | {status: "blocked", pending: [...], logs}.
"use strict";

const vm = require("vm");
const crypto = require("crypto");

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
  process.exit(0);
}

// ------------------------------------------------------------ meta

const META_HEAD = /^(?:\s|\/\/[^\n]*\n|\/\*[\s\S]*?\*\/)*export\s+const\s+meta\s*=\s*(?=\{)/;

// The end of the object literal starting at `start` (a "{"), skipping
// strings, template literals and comments; -1 when it never closes.
function literalEnd(src, start) {
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if (c === "'" || c === '"' || c === "`") {
      for (i++; i < src.length && src[i] !== c; i++) if (src[i] === "\\") i++;
    } else if (c === "/" && src[i + 1] === "/") {
      while (i < src.length && src[i] !== "\n") i++;
    } else if (c === "/" && src[i + 1] === "*") {
      i = src.indexOf("*/", i + 2);
      if (i < 0) return -1;
      i++;
    } else if (c === "{" || c === "[") depth++;
    else if (c === "}" || c === "]") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

function readMeta(src) {
  const head = META_HEAD.exec(src);
  if (!head) return {error: "`export const meta = { name, description, phases }` must be the first statement in the script"};
  const start = head[0].length;
  const end = literalEnd(src, start);
  if (end < 0) return {error: "meta is not a closed object literal"};
  const literal = src.slice(start, end);
  if (/\$\{/.test(literal.replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"/g, ""))) {
    return {error: "meta must be a pure literal: template interpolation is not allowed"};
  }
  if (/\.\.\./.test(literal.replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`/g, ""))) {
    return {error: "meta must be a pure literal: spread is not allowed"};
  }
  let meta;
  try {
    // An empty context: any variable or call in the literal throws.
    meta = vm.runInNewContext("(" + literal + ")", Object.create(null), {timeout: 1000});
  } catch (e) {
    return {error: "meta must be a pure literal: " + (e && e.message ? e.message : String(e))};
  }
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) return {error: "meta must be an object"};
  if (typeof meta.name !== "string" || !meta.name) return {error: "meta.name is required (a string)"};
  if (typeof meta.description !== "string" || !meta.description) return {error: "meta.description is required (a string)"};
  if (meta.phases !== undefined && (!Array.isArray(meta.phases) || meta.phases.some(p => !p || typeof p.title !== "string"))) {
    return {error: "meta.phases must be a list of {title, detail?}"};
  }
  const body = src.slice(end).replace(/^[;\s]*\n/, "").trimStart();
  return {meta: JSON.parse(JSON.stringify(meta)), body};
}

// ------------------------------------------------------------ run

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value).sort()) out[k] = stable(value[k]);
    return out;
  }
  return value;
}

function checkSchema(schema) {
  if (!schema || typeof schema !== "object" || schema.type !== "object" || !schema.properties || typeof schema.properties !== "object") {
    throw new Error("agent(): opts.schema needs {type: 'object', properties: {...}} at its root");
  }
  for (const r of schema.required || []) {
    if (!(r in schema.properties)) throw new Error(`agent(): schema requires '${r}', which is not among its properties`);
  }
}

function run(input, meta, body) {
  const results = input.results || {};
  const failed = new Set(input.failed || []);
  const maxItems = input.maxItems || 4096;
  const pending = [];
  const logs = [];
  const seen = new Map();
  let phase = null;

  function agent(prompt, opts) {
    return new Promise((resolve, reject) => {
      if (typeof prompt !== "string") return reject(new Error("agent(): prompt must be a string"));
      const o = opts && typeof opts === "object" ? opts : {};
      if (o.schema !== undefined) {
        try { checkSchema(o.schema); } catch (e) { return reject(e); }
      }
      const options = {};
      for (const k of ["schema", "model", "effort", "isolation", "agentType"]) if (o[k] !== undefined) options[k] = o[k];
      const base = crypto.createHash("sha256").update(JSON.stringify([prompt, stable(options)])).digest("hex").slice(0, 24);
      const n = seen.get(base) || 0;
      seen.set(base, n + 1);
      const key = `${base}-${n}`;
      if (failed.has(key)) return resolve(null);
      if (Object.prototype.hasOwnProperty.call(results, key)) return resolve(inside(results[key]));
      pending.push({key, prompt, options, label: typeof o.label === "string" ? o.label : null,
                    phase: typeof o.phase === "string" ? o.phase : phase});
      // Never settles in this process: the run continues once it has a result.
    });
  }

  function guard(list, what) {
    if (!Array.isArray(list)) throw new Error(`${what}: expects an array`);
    if (list.length > maxItems) throw new Error(`${what}: at most ${maxItems} items in one call`);
  }

  function parallel(thunks) {
    guard(thunks, "parallel()");
    return Promise.all(thunks.map(t => Promise.resolve().then(() => t()).catch(() => null)));
  }

  function pipeline(items, ...stages) {
    guard(items, "pipeline()");
    // Each item runs through every stage on its own; a stage that throws
    // drops that item to null and skips its later stages.
    return Promise.all(items.map((item, i) => stages.reduce(
      (prev, stage) => prev.then(v => stage(v, item, i)), Promise.resolve(item))
      .catch(() => null)));
  }

  const sandbox = {
    agent, parallel, pipeline,
    phase: (title) => { phase = String(title); },
    log: (message) => { logs.push(String(message)); },
    args: undefined,
    budget: Object.freeze({total: null, spent: () => input.spent || 0, remaining: () => Infinity}),
    workflow: () => { throw new Error("workflow(): nested workflows are not supported"); },
    console: {log: (...a) => logs.push(a.join(" "))},
  };
  const context = vm.createContext(sandbox, {codeGeneration: {strings: false, wasm: false}});
  vm.runInContext(`
    Math.random = () => { throw new Error("Math.random() is not available in a workflow (it would break resume); vary prompts by index instead"); };
    (() => {
      const RealDate = Date;
      function WorkflowDate(...a) {
        if (a.length === 0) throw new Error("new Date() without arguments is not available in a workflow (it would break resume); pass timestamps in args");
        return new.target ? new RealDate(...a) : RealDate(...a);
      }
      WorkflowDate.prototype = RealDate.prototype;
      WorkflowDate.UTC = RealDate.UTC;
      WorkflowDate.parse = RealDate.parse;
      WorkflowDate.now = () => { throw new Error("Date.now() is not available in a workflow (it would break resume); pass timestamps in args"); };
      globalThis.Date = WorkflowDate;
    })();
  `, context);

  // Values the script sees are made in its own context (an array passes
  // instanceof Array there).
  const parse = vm.runInContext("JSON.parse", context);
  function inside(value) { return value === undefined ? undefined : parse(JSON.stringify(value)); }
  context.args = inside(input.args);

  let settled = false;
  let main;
  try {
    main = vm.runInContext(`(async () => {\n${body}\n})()`, context, {timeout: 10000, filename: "workflow.js"});
  } catch (e) {
    emit({status: "error", error: e && e.message ? e.message : String(e), logs});
  }
  main.then(
    value => { settled = true; emit({status: "done", value: value === undefined ? null : value, logs}); },
    error => { settled = true; emit({status: "error", error: error && error.message ? error.message : String(error), logs}); });
  process.on("beforeExit", () => {
    if (!settled) emit({status: "blocked", pending, logs});
  });
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { raw += chunk; });
process.stdin.on("end", () => {
  let input;
  try { input = JSON.parse(raw); } catch (e) { emit({error: "bad input"}); }
  const parsed = readMeta(String(input.script || ""));
  if (parsed.error) emit({error: parsed.error});
  if (input.mode === "check") emit({meta: parsed.meta});
  run(input, parsed.meta, parsed.body);
});
