// The local `noeta serve` the app runs: finding the binary, its arguments, a
// free port, a fresh token, waiting until it answers, and stopping it.
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { accessSync, constants, statSync } from "node:fs";
import { createServer } from "node:net";
import { homedir } from "node:os";
import { delimiter, join, relative, isAbsolute } from "node:path";

/** What to run when `noeta` is missing (`serve` needs the server extra). */
export const INSTALL_LINE = "uv tool install 'noeta[server] @ git+https://github.com/initxy/noeta'";

function executable(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** Where `noeta` is looked for besides PATH: an app started from the Dock or a
 * desktop file does not get the login shell's PATH. */
export function extraBinDirs(home = homedir()): string[] {
  return [join(home, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"];
}

/** The `noeta` to run: the configured path (it must exist), else the first on
 * PATH or in {@link extraBinDirs}; `null` when there is none. */
export function findNoeta(configured: string | undefined, path = process.env.PATH ?? "", home = homedir()): string | null {
  if (configured) return executable(configured) ? configured : null;
  const names = process.platform === "win32" ? ["noeta.exe", "noeta.cmd", "noeta"] : ["noeta"];
  const dirs = [...path.split(delimiter).filter(Boolean), ...extraBinDirs(home)];
  for (const dir of dirs) for (const name of names) if (executable(join(dir, name))) return join(dir, name);
  return null;
}

/** A port nothing listens on now (the OS picks it). */
export function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      srv.close(() => resolve(port));
    });
  });
}

/** A bearer token made for this run only. */
export function freshToken(): string {
  return randomBytes(32).toString("base64url");
}

/** Whether `dir` is `root` or below it. */
export function isUnder(dir: string, root: string): boolean {
  const rel = relative(root, dir);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

export interface ServeOptions {
  noeta: string;
  port: number;
  token: string;
  /** The built web app the server also serves. */
  webDir: string;
  /** The server's own directory: where a session without a folder runs. */
  cwd: string;
  /** Directories besides `cwd` sessions may run in. */
  roots: string[];
}

/** `noeta serve`'s arguments. The token is not among them (a process list
 * shows arguments): it goes in the environment, see {@link serveEnv}. */
export function serveArgs(o: ServeOptions): string[] {
  const roots = o.roots.filter((r) => !isUnder(r, o.cwd));
  return [
    "serve",
    "--host", "127.0.0.1",
    "--port", String(o.port),
    "--web", o.webDir,
    ...[...new Set(roots)].flatMap((r) => ["--allow-root", r]),
  ];
}

/** The server's environment: this app's, plus the token it checks. */
export function serveEnv(token: string, base: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  return { ...base, NOETA_SERVER_TOKEN: token };
}

/** What the page is given: its server and token (`window.NOETA_CONFIG`). */
export interface PageConfig {
  server: string;
  token: string;
}

export interface Running {
  url: string;
  token: string;
  child: ChildProcess;
  /** The server's last output lines, for an error window. */
  output(): string;
  stop(): Promise<void>;
}

export type Spawn = typeof spawn;
export type Fetch = (url: string, init: { headers: Record<string, string> }) => Promise<{ status: number }>;

export class ServerError extends Error {
  constructor(
    message: string,
    readonly output = "",
  ) {
    super(message);
  }
}

/** Starts `noeta serve` and resolves once its `/agents` answers with the
 * token; rejects when it exits first or does not answer in `timeoutMs`. */
export async function startServer(
  o: ServeOptions,
  deps: { spawn?: Spawn; fetch?: Fetch; timeoutMs?: number; env?: NodeJS.ProcessEnv } = {},
): Promise<Running> {
  const run = deps.spawn ?? spawn;
  const get = deps.fetch ?? ((u, init) => fetch(u, init));
  const url = `http://127.0.0.1:${o.port}`;
  const child = run(o.noeta, serveArgs(o), {
    cwd: o.cwd,
    env: serveEnv(o.token, deps.env),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let tail = "";
  const keep = (b: Buffer) => {
    tail = (tail + b.toString()).slice(-4000);
  };
  child.stdout?.on("data", keep);
  child.stderr?.on("data", keep);
  let exited: string | null = null;
  const exit = new Promise<void>((resolve) => {
    child.once("exit", (code, signal) => {
      exited = signal ? `signal ${signal}` : `code ${code}`;
      resolve();
    });
    child.once("error", (e) => {
      exited = e.message;
      resolve();
    });
  });
  const stop = async () => {
    if (exited !== null) return;
    child.kill("SIGTERM");
    const killer = setTimeout(() => child.kill("SIGKILL"), 5000);
    await exit;
    clearTimeout(killer);
  };
  const deadline = Date.now() + (deps.timeoutMs ?? 120_000);
  while (exited === null) {
    try {
      const resp = await get(`${url}/agents`, { headers: { authorization: `Bearer ${o.token}` } });
      if (resp.status === 200) return { url, token: o.token, child, output: () => tail, stop };
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) {
      await stop();
      throw new ServerError("noeta serve did not answer in time", tail);
    }
    await Promise.race([exit, new Promise((r) => setTimeout(r, 150))]);
  }
  throw new ServerError(`noeta serve exited (${exited})`, tail);
}
