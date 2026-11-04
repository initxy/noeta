import { EventEmitter } from "node:events";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  findNoeta,
  freePort,
  freshToken,
  INSTALL_LINE,
  isUnder,
  serveArgs,
  serveEnv,
  ServerError,
  startServer,
  type ServeOptions,
  type Spawn,
} from "../src/server";

function bin(dir: string, name = "noeta"): string {
  mkdirSync(dir, { recursive: true });
  const path = join(dir, name);
  writeFileSync(path, "#!/bin/sh\n");
  chmodSync(path, 0o755);
  return path;
}

const options: ServeOptions = {
  noeta: "/opt/noeta",
  port: 4321,
  token: "tok",
  webDir: "/app/web",
  cwd: "/home/me",
  roots: [],
};

/** A child process that exits when killed, or with `exitCode` right away. */
function fakeChild(exitCode?: number) {
  const child = Object.assign(new EventEmitter(), {
    stdout: new EventEmitter(),
    stderr: new EventEmitter(),
    killed: [] as string[],
    kill(sig: string) {
      this.killed.push(sig);
      setImmediate(() => child.emit("exit", null, sig));
      return true;
    },
  });
  if (exitCode !== undefined)
    setImmediate(() => {
      child.stderr.emit("data", Buffer.from("noeta: no model configured\n"));
      child.emit("exit", exitCode, null);
    });
  return child;
}

describe("findNoeta", () => {
  const root = mkdtempSync(join(tmpdir(), "noeta-desktop-find-"));

  it("takes the first executable on PATH", () => {
    const a = join(root, "a");
    mkdirSync(a);
    writeFileSync(join(a, "noeta"), "not executable");
    const b = bin(join(root, "b"));
    expect(findNoeta(undefined, `${a}:${join(root, "b")}`, join(root, "nohome"))).toBe(b);
  });

  it("looks in ~/.local/bin, which an app opened from the Dock has no PATH for", () => {
    const home = join(root, "home");
    const local = bin(join(home, ".local", "bin"));
    expect(findNoeta(undefined, "", home)).toBe(local);
  });

  it("uses a configured path only when it exists", () => {
    const own = bin(join(root, "own"), "my-noeta");
    expect(findNoeta(own, "", join(root, "nohome"))).toBe(own);
    expect(findNoeta(join(root, "missing"), `${join(root, "b")}`, join(root, "nohome"))).toBeNull();
  });

  it("is null when there is none", () => {
    expect(findNoeta(undefined, join(root, "empty"), join(root, "nohome"))).toBeNull();
  });

  it("names an install line with the server extra", () => {
    expect(INSTALL_LINE).toContain("noeta[server]");
  });
});

describe("serve arguments", () => {
  it("binds loopback on the given port and serves the web build", () => {
    expect(serveArgs(options)).toEqual(["serve", "--host", "127.0.0.1", "--port", "4321", "--web", "/app/web"]);
  });

  it("never carries the token; the environment does", () => {
    expect(serveArgs(options).join(" ")).not.toContain("tok");
    expect(serveEnv("tok", { PATH: "/bin" })).toEqual({ PATH: "/bin", NOETA_SERVER_TOKEN: "tok" });
  });

  it("allows each root outside the server's directory once", () => {
    const args = serveArgs({ ...options, roots: ["/srv/a", "/home/me/code", "/srv/a"] });
    expect(args.slice(7)).toEqual(["--allow-root", "/srv/a"]);
  });

  it("isUnder compares whole path segments", () => {
    expect(isUnder("/home/me", "/home/me")).toBe(true);
    expect(isUnder("/home/me/x", "/home/me")).toBe(true);
    expect(isUnder("/home/meow", "/home/me")).toBe(false);
  });
});

describe("port and token", () => {
  it("a free port is a real port", async () => {
    const port = await freePort();
    expect(port).toBeGreaterThan(0);
    expect(port).toBeLessThan(65536);
  });

  it("a token is fresh each time and URL-safe", () => {
    const a = freshToken();
    expect(a).toMatch(/^[\w-]{40,}$/);
    expect(freshToken()).not.toBe(a);
  });
});

describe("startServer", () => {
  it("spawns noeta serve in the server's directory and waits until /agents takes the token", async () => {
    const child = fakeChild();
    const spawn = vi.fn(() => child) as unknown as Spawn;
    let calls = 0;
    const fetch = vi.fn(async (_url: string, init: { headers: Record<string, string> }) => {
      calls += 1;
      if (calls < 3) throw new Error("ECONNREFUSED");
      return { status: init.headers.authorization === "Bearer tok" ? 200 : 401 };
    });
    const running = await startServer(options, { spawn, fetch, env: { HOME: "/home/me" } });
    expect(running.url).toBe("http://127.0.0.1:4321");
    expect(running.token).toBe("tok");
    const [cmd, args, opts] = (spawn as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(cmd).toBe("/opt/noeta");
    expect(args).toEqual(serveArgs(options));
    expect(opts).toMatchObject({ cwd: "/home/me", env: { HOME: "/home/me", NOETA_SERVER_TOKEN: "tok" } });
    expect(fetch).toHaveBeenLastCalledWith("http://127.0.0.1:4321/agents", { headers: { authorization: "Bearer tok" } });
    await running.stop();
    expect(child.killed).toEqual(["SIGTERM"]);
    await running.stop(); // stopping twice is harmless
    expect(child.killed).toEqual(["SIGTERM"]);
  });

  it("tells how a running server ended, when it dies on its own", async () => {
    const child = fakeChild();
    const spawn = vi.fn(() => child) as unknown as Spawn;
    const running = await startServer(options, { spawn, fetch: async () => ({ status: 200 }) });
    child.stderr.emit("data", Buffer.from("Traceback: boom\n"));
    child.emit("exit", null, "SIGKILL");
    expect(await running.exited).toBe("signal SIGKILL");
    expect(running.output()).toContain("boom");
    await running.stop(); // already gone: nothing to kill
    expect(child.killed).toEqual([]);
  });

  it("rejects with the server's output when it exits first", async () => {
    const spawn = vi.fn(() => fakeChild(2)) as unknown as Spawn;
    const fetch = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    });
    const err = await startServer(options, { spawn, fetch }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ServerError);
    expect((err as ServerError).message).toContain("exited (code 2)");
    expect((err as ServerError).output).toContain("no model configured");
  });

  it("stops a server that does not answer in time", async () => {
    const child = fakeChild();
    const spawn = vi.fn(() => child) as unknown as Spawn;
    const fetch = vi.fn(async () => ({ status: 503 }));
    const err = await startServer(options, { spawn, fetch, timeoutMs: 50 }).catch((e: unknown) => e);
    expect((err as Error).message).toContain("did not answer in time");
    expect(child.killed).toEqual(["SIGTERM"]);
  });

  it("rejects when the binary cannot be run", async () => {
    const child = fakeChild();
    const spawn = vi.fn(() => {
      setImmediate(() => child.emit("error", new Error("spawn EACCES")));
      return child;
    }) as unknown as Spawn;
    const err = await startServer(options, { spawn, fetch: async () => Promise.reject(new Error("no")) }).catch((e: unknown) => e);
    expect((err as Error).message).toContain("spawn EACCES");
  });
});
