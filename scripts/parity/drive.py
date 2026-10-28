"""Drive an interactive Claude Code through one scenario session in tmux.

Claude Code runs in a tmux session (locally, or on another machine with
``--ssh HOST``) with a fresh config directory and ``ANTHROPIC_BASE_URL``
pointing at the recording proxy (``record.py``), which runs on this machine
and writes into ``OUT``. Each step of the session (``scenarios.json``) is
typed into the pane; the driver waits until the pane is idle and no new
recording arrived for 15 s, answers permission prompts with the first choice,
then presses the step's ``then`` keys. ``OUT.marks`` gets one line per step:
``=== <step> @ <recordings so far>``.

    python scripts/parity/drive.py start m1 --port 18801 --project ~/cc-rec/proj [--ssh mac]
    python scripts/parity/drive.py run m1 --out ~/cc-recordings/2.1.400/m1 [--ssh mac]

``start`` uses a placeholder token: the proxy's ``--auth-env`` supplies the
real one, so no credential reaches the machine Claude Code runs on. With
``--ssh``, the proxy's port must be reachable there (``ssh -R PORT:127.0.0.1:PORT``).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUSY = re.compile(r"esc to interrupt")
PROMPT = re.compile("Do you want to|Would you like to proceed|\u276f 1\\. Yes")


def scenarios() -> dict[str, Any]:
    return json.loads((HERE / "scenarios.json").read_text("utf-8"))


class Pane:
    def __init__(self, name: str, ssh: str | None, tmux: str) -> None:
        self.name, self.ssh, self.tmux = name, ssh, tmux

    def sh(self, *args: str) -> str:
        cmd = [self.tmux, *args]
        if self.ssh:
            cmd = ["ssh", self.ssh, shlex.join(cmd)]
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout

    def type(self, text: str) -> None:
        self.sh("send-keys", "-t", self.name, "-l", text)
        time.sleep(0.7)
        self.sh("send-keys", "-t", self.name, "Enter")

    def key(self, key: str) -> None:
        self.sh("send-keys", "-t", self.name, key)

    def text(self) -> str:
        return self.sh("capture-pane", "-t", self.name, "-p")


def start(args: argparse.Namespace) -> None:
    sess = scenarios()["sessions"][args.session]
    if not args.project.startswith("/"):
        raise SystemExit("drive: --project must be an absolute path on the host")
    # A resumed session reuses the config directory (and so the transcript) of its original.
    cfg = args.config or f"{args.project}/../cfg-{sess.get('resume') or args.session}"
    setup = {"hasCompletedOnboarding": True, "theme": "dark", "projects": {args.project: {
        "hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
        "enabledMcpjsonServers": ["echo"]}}}
    env = {"HOME": "$HOME", "USER": "$USER", "PATH": args.path, "SHELL": args.shell,
           "LANG": "en_US.UTF-8", "TERM": "xterm-256color", "NO_PROXY": "localhost,127.0.0.1",
           "CLAUDE_CONFIG_DIR": cfg, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{args.port}",
           "ANTHROPIC_AUTH_TOKEN": "placeholder", **sess.get("env", {})}
    claude = shlex.join(["--model", scenarios()["model"], *sess.get("cc_args", [])])
    # Values starting with $ expand on the host.
    inner = ("env -i " + " ".join(f"{k}={v if v.startswith('$') else shlex.quote(v)}"
                                  for k, v in env.items())
             + f" {args.claude} {claude}; sleep 3600")
    script = [f"{args.tmux} kill-session -t {args.session} 2>/dev/null"]
    if not sess.get("resume"):
        script += [f"rm -rf {shlex.quote(cfg)}", f"mkdir -p {shlex.quote(cfg)}",
                   f"echo {shlex.quote(json.dumps(setup))} > {shlex.quote(cfg)}/.claude.json"]
    script.append(f"{args.tmux} new-session -d -s {args.session} -x 200 -y 50 "
                  f"-c {shlex.quote(args.project)} {shlex.quote(inner)}")
    cmd = " && ".join(script[1:])
    full = f"{script[0]}; {cmd}"
    run = ["ssh", args.ssh, full] if args.ssh else ["sh", "-c", full]
    subprocess.run(run, check=True)


def wait_idle(pane: Pane, out: Path, max_wait: int, approvals: Path) -> None:
    last, stable, waited = -1, 0, 0
    while waited < max_wait:
        time.sleep(5)
        waited += 5
        n = len(list(out.glob("*.json")))
        text = pane.text()
        if PROMPT.search(text):
            with approvals.open("a") as f:
                f.write("\n".join(ln for ln in text.splitlines() if PROMPT.search(ln)) + "\n")
            pane.key("Enter")
            stable = 0
            continue
        busy = bool(BUSY.search(text))
        stable = stable + 5 if n == last and not busy else 0
        last = n
        if stable >= 15:
            return


def run(args: argparse.Namespace) -> None:
    sess = scenarios()["sessions"][args.session]
    pane = Pane(args.session, args.ssh, args.tmux)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    marks = out.with_name(out.name + ".marks")
    approvals = out.with_name(out.name + ".approvals")
    for step in sess["steps"]:
        if args.skip_warmup and step["name"] == "warmup":
            continue
        with marks.open("a") as f:
            f.write(f"=== {step['name']} @ {len(list(out.glob('*.json')))}\n")
        pane.type(step["say"])
        wait_idle(pane, out, int(step.get("wait", 500)), approvals)
        for key, pause in step.get("then", []):
            pane.key(key)
            time.sleep(pause)
        print(f"== {step['name']}: {len(list(out.glob('*.json')))} recordings", flush=True)
    with marks.open("a") as f:
        f.write(f"=== end @ {len(list(out.glob('*.json')))}\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Drive Claude Code through a scenario in tmux.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "run"):
        p = sub.add_parser(name)
        p.add_argument("session")
        p.add_argument("--ssh", help="run tmux on this host")
        p.add_argument("--tmux", default="tmux", help="tmux binary on the host")
    s = sub.choices["start"]
    s.add_argument("--port", type=int, required=True, help="the recording proxy's port")
    s.add_argument("--project", required=True,
                   help="the scenario project on the host (absolute path)")
    s.add_argument("--config", help="CLAUDE_CONFIG_DIR (default: cfg-<session> beside the project)")
    s.add_argument("--claude", default="$HOME/.local/bin/claude")
    s.add_argument("--path", default="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    s.add_argument("--shell", default="/bin/zsh")
    r = sub.choices["run"]
    r.add_argument("--out", type=Path, required=True, help="the proxy's recording directory")
    r.add_argument("--skip-warmup", action="store_true")
    args = ap.parse_args(argv)
    (start if args.cmd == "start" else run)(args)


if __name__ == "__main__":
    main()
