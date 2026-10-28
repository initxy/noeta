// Runs one Workflow script as far as the agent results known so far allow,
// inside an embedded V8 context that engine.py creates for this one replay.
//
// This file is one expression: it evaluates to {split, run}, functions
// engine.py holds and calls. split(script) cuts a script into its meta
// literal and its body. run(input) turns the context into the workflow
// sandbox: the workflow API as globals, the clock, randomness, timers, code
// generation from strings and the other non-deterministic globals gone.
// engine.py then evaluates `__workflow_watch((async () => {body})())`;
// agent() returns the recorded result of a call it has seen before (keyed by
// its prompt, options and occurrence) and never settles otherwise. Once the
// microtasks have drained, `__workflow_finish()` reports where the script
// got: {status: "done", value, logs} | {status: "error", error, logs} |
// {status: "blocked", pending: [...], logs}. Both are evaluated, not called
// from Python: mini-racer stalls a function call queued behind a script that
// never yields, while an evaluation's time limit still stops it. The script
// can see the two, but reading or calling them changes only its own outcome.
// Replaying the same script with more results moves it further, so a run
// survives restarts: only finished agent results are kept, never this
// context.
(function () {
  "use strict";

  // The script shares this context and may replace any builtin; the harness
  // keeps its own references.
  const stringify = JSON.stringify;
  const parse = JSON.parse;
  const keys = Object.keys;
  const isArray = Array.isArray;
  const hasOwn = Object.prototype.hasOwnProperty;
  const freeze = Object.freeze;
  const defineProperty = Object.defineProperty;
  const getPrototypeOf = Object.getPrototypeOf;
  const RealDate = Date;
  const RealPromise = Promise;
  const RealError = Error;
  const RealMap = Map;
  const RealSet = Set;
  const toWellFormed = String.prototype.toWellFormed;
  const encodeUri = encodeURIComponent;
  const unescapeBytes = unescape;
  const charCodeAt = String.prototype.charCodeAt;
  const global = globalThis;
  const deleteProperty = Reflect.deleteProperty;

  // ---------------------------------------------------------- meta

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

  // {literal, body} or {error}. engine.py evaluates the literal in a context
  // stripped of every global, so any variable or call in it throws.
  function split(src) {
    src = String(src);
    const head = META_HEAD.exec(src);
    if (!head) return stringify({error: "`export const meta = { name, description, phases }` must be the first statement in the script"});
    const start = head[0].length;
    const end = literalEnd(src, start);
    if (end < 0) return stringify({error: "meta is not a closed object literal"});
    const literal = src.slice(start, end);
    if (/\$\{/.test(literal.replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"/g, ""))) {
      return stringify({error: "meta must be a pure literal: template interpolation is not allowed"});
    }
    if (/\.\.\./.test(literal.replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`/g, ""))) {
      return stringify({error: "meta must be a pure literal: spread is not allowed"});
    }
    // No code at all: a call, function or getter would run while the meta is
    // read, outside a replay's guards.
    if (/[()]|=>/.test(literal.replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`|\/\/[^\n]*|\/\*[\s\S]*?\*\//g, ""))) {
      return stringify({error: "meta must be a pure literal: calls and functions are not allowed"});
    }
    const body = src.slice(end).replace(/^[;\s]*\n/, "").trimStart();
    return stringify({literal, body});
  }

  // ---------------------------------------------------------- keys

  // SHA-256 of the UTF-8 bytes of a string, as hex (the agent-call keys a
  // run's recorded results are stored under).
  const K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];

  function sha256(text) {
    const bin = unescapeBytes(encodeUri(toWellFormed.call(text)));
    const n = bin.length;
    const words = new Array(((n + 9 + 63) >> 6) << 4).fill(0);
    for (let i = 0; i < n; i++) words[i >> 2] |= (charCodeAt.call(bin, i) & 0xff) << (24 - (i % 4) * 8);
    words[n >> 2] |= 0x80 << (24 - (n % 4) * 8);
    words[words.length - 1] = n * 8;
    words[words.length - 2] = (n / 0x20000000) >>> 0;
    const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    const w = new Array(64);
    const rotr = (x, r) => (x >>> r) | (x << (32 - r));
    for (let off = 0; off < words.length; off += 16) {
      for (let t = 0; t < 64; t++) {
        if (t < 16) w[t] = words[off + t] | 0;
        else {
          const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
          const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
          w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
        }
      }
      let [a, b, c, d, e, f, g, hh] = h;
      for (let t = 0; t < 64; t++) {
        const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[t] + w[t]) | 0;
        const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
      h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
    }
    return h.map(x => (x >>> 0).toString(16).padStart(8, "0")).join("");
  }

  function stable(value) {
    if (isArray(value)) return value.map(stable);
    if (value && typeof value === "object") {
      const out = {};
      for (const k of keys(value).sort()) out[k] = stable(value[k]);
      return out;
    }
    return value;
  }

  function checkSchema(schema) {
    if (!schema || typeof schema !== "object" || schema.type !== "object" || !schema.properties || typeof schema.properties !== "object") {
      throw new RealError("agent(): opts.schema needs {type: 'object', properties: {...}} at its root");
    }
    for (const r of schema.required || []) {
      if (!(r in schema.properties)) throw new RealError(`agent(): schema requires '${r}', which is not among its properties`);
    }
  }

  // ---------------------------------------------------------- run

  const NO_CODEGEN = "Code generation from strings is not available in a workflow";
  // Globals that read the clock, schedule work for later, share memory or
  // depend on the garbage collector: a replay must see the same world every
  // time, or resume breaks.
  const REMOVED = ["setTimeout", "clearTimeout", "__timer_manager", "Atomics", "SharedArrayBuffer",
                   "WebAssembly", "Intl", "Temporal", "FinalizationRegistry", "WeakRef"];

  let pending = [];
  let logs = [];
  let outcome = null;

  function message(e) {
    return e && typeof e.message === "string" ? e.message : String(e);
  }

  function lockDown() {
    // `var` globals (the timer shim) cannot be deleted; they become undefined.
    for (const name of REMOVED) if (!deleteProperty(global, name)) global[name] = undefined;
    const blocked = function () { throw new EvalError(NO_CODEGEN); };
    global.eval = blocked;
    // Every function's constructor compiles a string: Function, and the
    // async, generator and async generator kinds reached through
    // (async () => {}).constructor and the like.
    const kinds = [function () {}, async function () {}, function* () {}, async function* () {}];
    for (const fn of kinds) {
      defineProperty(getPrototypeOf(fn), "constructor", {value: blocked, writable: false, configurable: false});
    }
    blocked.prototype = Function.prototype;
    global.Function = blocked;
    global.Math.random = () => { throw new RealError("Math.random() is not available in a workflow (it would break resume); vary prompts by index instead"); };
    function WorkflowDate(...a) {
      if (a.length === 0) throw new RealError("new Date() without arguments is not available in a workflow (it would break resume); pass timestamps in args");
      return new.target ? new RealDate(...a) : RealDate(...a);
    }
    WorkflowDate.prototype = RealDate.prototype;
    WorkflowDate.UTC = RealDate.UTC;
    WorkflowDate.parse = RealDate.parse;
    WorkflowDate.now = () => { throw new RealError("Date.now() is not available in a workflow (it would break resume); pass timestamps in args"); };
    defineProperty(RealDate.prototype, "constructor", {value: WorkflowDate});
    global.Date = WorkflowDate;
  }

  function run(inputJson) {
    const input = parse(inputJson);
    const results = input.results || {};
    const failed = new RealSet(input.failed || []);
    const maxItems = input.maxItems || 4096;
    const seen = new RealMap();
    let phase = null;

    function agent(prompt, opts) {
      return new RealPromise((resolve, reject) => {
        if (typeof prompt !== "string") return reject(new RealError("agent(): prompt must be a string"));
        const o = opts && typeof opts === "object" ? opts : {};
        if (o.schema !== undefined) {
          try { checkSchema(o.schema); } catch (e) { return reject(e); }
        }
        const options = {};
        for (const k of ["schema", "model", "effort", "isolation", "agentType"]) if (o[k] !== undefined) options[k] = o[k];
        const base = sha256(stringify([prompt, stable(options)])).slice(0, 24);
        const n = seen.get(base) || 0;
        seen.set(base, n + 1);
        const key = `${base}-${n}`;
        if (failed.has(key)) return resolve(null);
        if (hasOwn.call(results, key)) {
          const value = results[key];
          return resolve(value === undefined ? undefined : parse(stringify(value)));
        }
        pending.push({key, prompt, options: parse(stringify(options)),
                      label: typeof o.label === "string" ? o.label : null,
                      phase: typeof o.phase === "string" ? o.phase : phase});
        // Never settles in this replay: the run continues once it has a result.
      });
    }

    function guard(list, what) {
      if (!isArray(list)) throw new RealError(`${what}: expects an array`);
      if (list.length > maxItems) throw new RealError(`${what}: at most ${maxItems} items in one call`);
    }

    function parallel(thunks) {
      guard(thunks, "parallel()");
      return RealPromise.all(thunks.map(t => RealPromise.resolve().then(() => t()).catch(() => null)));
    }

    function pipeline(items, ...stages) {
      guard(items, "pipeline()");
      // Each item runs through every stage on its own; a stage that throws
      // drops that item to null and skips its later stages.
      return RealPromise.all(items.map((item, i) => stages.reduce(
        (prev, stage) => prev.then(v => stage(v, item, i)), RealPromise.resolve(item))
        .catch(() => null)));
    }

    lockDown();
    const api = {
      agent, parallel, pipeline,
      phase: (title) => { phase = String(title); },
      log: (text) => { logs.push(String(text)); },
      args: input.args === undefined ? undefined : parse(stringify(input.args)),
      budget: freeze({total: null, spent: () => input.spent || 0, remaining: () => Infinity}),
      workflow: () => { throw new RealError("workflow(): nested workflows are not supported"); },
      console: freeze({log: (...a) => { logs.push(a.join(" ")); }}),
    };
    for (const name of keys(api)) {
      defineProperty(global, name, {value: api[name], writable: true, configurable: true, enumerable: false});
    }
    for (const [name, fn] of [["__workflow_watch", watch], ["__workflow_finish", finish]]) {
      defineProperty(global, name, {value: fn, writable: false, configurable: false, enumerable: false});
    }
  }

  let watched = false;

  function watch(promise) {
    if (watched) return;
    watched = true;
    RealPromise.resolve(promise).then(
      value => { outcome = {status: "done", value: value === undefined ? null : value}; },
      error => { outcome = {status: "error", error: message(error)}; });
  }

  function finish() {
    if (outcome === null) return stringify({status: "blocked", pending, logs});
    if (outcome.status === "error") return stringify({status: "error", error: outcome.error, logs});
    try {
      return stringify({status: "done", value: outcome.value, logs});
    } catch (e) {
      return stringify({status: "error", error: "the workflow's return value is not JSON: " + message(e), logs});
    }
  }

  return {split, run};
})()
