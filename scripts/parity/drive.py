"""Drive an interactive Claude Code through one scenario session in tmux.

Claude Code runs in a tmux session (locally, or on another machine with
``--ssh HOST``) with a fresh config directory and ``ANTHROPIC_BASE_URL``
pointing at the recording proxy (``record.py``), which runs on this machine
and writes into ``OUT``.

Each step of the session (``scenarios.json``) is typed only once Claude Code
is idle: no request in flight (the proxy's ``OUT/.live``), no "esc to
interrupt" in the pane, and no new recording for 15 s. A card (a permission
prompt, the plan approval, a question) is saved to ``OUT.cards/`` and
answered with the step's next ``cards`` key (``1``: yes, ``2``: yes and
don't ask again, ``Escape``: no; default ``Enter``, the highlighted first
choice). A step with ``interrupt`` presses Escape once its reply is
streaming. ``OUT.marks`` gets one line per step: ``=== <step> @ <recordings
so far>``, plus a warning when a typed prompt started no request.

    python scripts/parity/drive.py start m1 --port 18801 --project /Users/me/cc-rec/m1/proj [--ssh mac]
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
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUSY = re.compile(r"esc to interrupt")
CARD = re.compile("Do you want to|Would you like to proceed|Enter to select|\u276f 1\\.")
#: What Claude Code prints when a turn is over.
DONE = re.compile(r"· done \d|Interrupted|Compacted")
#: Bytes a reply must have streamed before an interrupt step presses Escape.
INTERRUPT_AFTER_BYTES = 2000
IDLE_S = 15
POLL_S = 3


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

    def history(self) -> str:
        """The pane with its scrollback."""
        return self.sh("capture-pane", "-t", self.name, "-p", "-S", "-2000")


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


class Driver:
    def __init__(self, pane: Pane, out: Path) -> None:
        self.pane = pane
        self.out = out
        self.cards = out.with_name(out.name + ".cards")
        self.marks = out.with_name(out.name + ".marks")
        self.cards.mkdir(parents=True, exist_ok=True)

    def files(self) -> int:
        return len(list(self.out.glob("*.json")))

    def live(self) -> dict[str, int]:
        try:
            data = json.loads((self.out / ".live").read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def mark(self, line: str) -> None:
        with self.marks.open("a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def answer_card(self, text: str, step: str, keys: Iterator[str]) -> bool:
        tail = "\n".join(text.rstrip().splitlines()[-30:])
        if "Enter to continue" in tail:
            # A notice from Claude Code itself (not a step's card): saved, dismissed.
            n = len(list(self.cards.glob("*.txt"))) + 1
            (self.cards / f"{n:03d}-notice.txt").write_text(
                f"# notice during step {step}\n{tail}\n", "utf-8")
            self.pane.key("Enter")
            time.sleep(2)
            return True
        if not CARD.search(tail):
            return False
        if "[✔]" in tail and "Submit answers" not in tail:
            # A multi-select question with a choice ticked: on to the next tab
            # (the last one submits); Enter would only toggle the box again.
            self.pane.key("Right")
            time.sleep(2)
            return True
        key = next(keys, "Enter")
        n = len(list(self.cards.glob("*.txt"))) + 1
        (self.cards / f"{n:03d}-{step}.txt").write_text(
            f"# step {step}; answered with {key}\n{tail}\n", "utf-8")
        self.pane.key(key)
        time.sleep(2)
        return True

    def settle(self, step: str, keys: Iterator[str], max_wait: float, say: str = "",
               idle: float = IDLE_S) -> bool:
        """Until idle for ``idle`` seconds, answering cards; False on timeout.
        With ``say``, idle also needs the turn it started to show as over (a
        "done" or "Interrupted" line below the prompt) — Claude Code can be
        between requests for a while (compacting, counting tokens)."""
        deadline = time.monotonic() + max_wait
        last, stable = -1, 0.0
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            text = self.pane.text()
            if self.answer_card(text, step, keys):
                stable = 0
                continue
            busy = bool(BUSY.search(text)) or bool(self.live())
            need = idle
            if say and not busy:
                hist = self.pane.history()
                at = hist.rfind("\u276f " + say.replace("`", "")[:40])
                if at >= 0 and not DONE.search(hist[at:]):
                    # No "done" line (a turn resumed by a background agent's
                    # report ends without one): a longer quiet spell will do.
                    need = max(idle, 60)
            n = self.files()
            stable = stable + POLL_S if n == last and not busy else 0
            last = n
            if stable >= need:
                return True
        return False

    def prompt_sent(self, before: int, say: str) -> bool:
        """``say`` (or its slash command) is in a request recorded since ``before``."""
        needle = say.split()[0] if say.startswith("/") else say[:60]
        for f in sorted(self.out.glob("*.json"))[before:]:
            if json.dumps(needle)[1:-1] in f.read_text("utf-8", "replace"):
                return True
        return False

    def wait_visible(self, say: str, max_wait: float) -> bool:
        """Until a reply is streaming and some of it shows under the prompt
        (Escape before that puts the prompt back in the input box instead of
        interrupting a turn)."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            time.sleep(1)
            if not any(v >= INTERRUPT_AFTER_BYTES for v in self.live().values()):
                continue
            text = self.pane.text()
            at = text.rfind(say.replace("`", "")[:40])
            if at >= 0 and "⏺" in text[at:]:
                return True
        return False

    def clear_input(self, step: str) -> None:
        """Empty the input box if text was left in it."""
        lines = self.pane.text().rstrip().splitlines()
        rules = [i for i, ln in enumerate(lines) if ln.startswith("\u2500\u2500\u2500")]
        box = lines[rules[-2] + 1:rules[-1]] if len(rules) >= 2 else []
        if any(re.match("\u276f \\S", ln) for ln in box):
            self.pane.key("C-u")
            self.mark(f"!!! {step}: cleared text left in the input box")
            time.sleep(1)

    def wait_started(self, before: int, max_wait: float = 120) -> bool:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            if self.live() or self.files() > before:
                return True
            time.sleep(1)
        return False


def run(args: argparse.Namespace) -> None:
    sess = scenarios()["sessions"][args.session]
    s = Driver(Pane(args.session, args.ssh, args.tmux), args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    s.settle("start", iter(()), 120)
    for step in sess["steps"]:
        name = step["name"]
        if args.only and name not in args.only:
            continue
        keys = iter(step.get("cards", []))
        if not s.settle(name, keys, 600):
            s.mark(f"!!! {name}: Claude Code never went idle before the step")
        s.clear_input(name)
        before = s.files()
        s.mark(f"=== {name} @ {before}")
        s.pane.type(step["say"])
        if not s.wait_started(before):
            s.mark(f"!!! {name}: the prompt started no request")
        if step.get("interrupt"):
            if s.wait_visible(step["say"], 300):
                s.pane.key("Escape")
            else:
                s.mark(f"!!! {name}: no reply showed to interrupt")
        if not s.settle(name, keys, float(step.get("wait", 900)), say=step["say"],
                        idle=float(step.get("idle", IDLE_S))):
            s.mark(f"!!! {name}: still busy after {step.get('wait', 900)}s")
        if step["say"] != "/compact" and not s.prompt_sent(before, step["say"]):
            s.mark(f"!!! {name}: the prompt is in no recorded request")
        left = list(keys)
        if left:
            s.mark(f"!!! {name}: cards expected but not shown: {left}")
    s.mark(f"=== end @ {s.files()}")


def session(args: argparse.Namespace) -> None:
    """One whole session: a fresh project and config under ``ROOT/<session>``
    on the host (a resumed session reuses its original's), the recording
    proxy and an ssh tunnel to it, Claude Code, every step; then all of it
    stopped again."""
    sess = scenarios()["sessions"][args.session]
    base = sess.get("resume") or args.session
    home = f"{args.root.rstrip('/')}/{base}"
    project, cfg = f"{home}/proj", f"{home}/cfg"
    remote = ["ssh", args.ssh] if args.ssh else ["sh", "-c"]
    if not sess.get("resume"):
        flags = " ".join(f for f, on in (("--data", sess.get("data")),
                                          ("--hooks", sess.get("hooks"))) if on)
        script = (HERE / "project.py").read_bytes()
        subprocess.run([*remote, f"rm -rf {shlex.quote(home)} && python3 - "
                                 f"{shlex.quote(home)} {flags}"], input=script, check=True)
    out = args.out_root / args.session
    proxy_cmd = [sys.executable, "-I", str(HERE / "record.py"), str(out), "--port",
                 str(args.port), "--upstream", args.upstream, "--auth-env", "PARITY_UPSTREAM_TOKEN"]
    if args.model:
        proxy_cmd += ["--model", args.model]
    for e in args.effort or []:
        proxy_cmd += ["--effort", e]
    if args.drop_disabled_thinking:
        proxy_cmd.append("--drop-disabled-thinking")
    log = out.with_name(out.name + ".proxy.log")
    out.parent.mkdir(parents=True, exist_ok=True)
    procs = [subprocess.Popen(proxy_cmd, stdout=log.open("a"), stderr=subprocess.STDOUT)]
    forward = f"{args.port}:127.0.0.1:{args.port}"
    if args.ssh:
        # With ssh connection sharing the forward is handed to the master and
        # this exits 0 at once; without it, it stays up until terminated.
        procs.append(subprocess.Popen(
            ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-R", forward, args.ssh],
            stdout=subprocess.DEVNULL, stderr=log.open("a")))
    try:
        time.sleep(3)
        if any(p.poll() not in (None, 0) for p in procs) or procs[0].poll() is not None:
            raise SystemExit(f"drive: proxy or tunnel exited, see {log}")
        args.project, args.config = project, cfg
        start(args)
        time.sleep(8)
        args.out = out
        run(args)
    finally:
        Pane(args.session, args.ssh, args.tmux).sh("kill-session", "-t", args.session)
        for p in procs:
            p.terminate()
        if args.ssh:
            subprocess.run(["ssh", "-O", "cancel", "-R", forward, args.ssh],
                           capture_output=True, check=False)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Drive Claude Code through a scenario in tmux.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "run", "session"):
        p = sub.add_parser(name)
        p.add_argument("session")
        p.add_argument("--ssh", help="run tmux on this host")
        p.add_argument("--tmux", default="tmux", help="tmux binary on the host")
    for name in ("start", "session"):
        s = sub.choices[name]
        s.add_argument("--port", type=int, required=True, help="the recording proxy's port")
        s.add_argument("--claude", default="$HOME/.local/bin/claude")
        s.add_argument("--path",
                       default="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        s.add_argument("--shell", default="/bin/zsh")
    s = sub.choices["start"]
    s.add_argument("--project", required=True,
                   help="the scenario project on the host (absolute path)")
    s.add_argument("--config", help="CLAUDE_CONFIG_DIR (default: cfg-<session> beside the project)")
    for name in ("run", "session"):
        sub.choices[name].add_argument("--only", action="append", help="only these steps")
    sub.choices["run"].add_argument("--out", type=Path, required=True,
                                    help="the proxy's recording directory")
    s = sub.choices["session"]
    s.add_argument("--root", required=True,
                   help="absolute directory on the host for each session's project and config")
    s.add_argument("--out-root", type=Path, required=True, help="recording directory here")
    s.add_argument("--upstream", required=True)
    s.add_argument("--model", help="upstream model (record.py --model)")
    s.add_argument("--effort", action="append", help="record.py --effort FROM=TO")
    s.add_argument("--drop-disabled-thinking", action="store_true")
    args = ap.parse_args(argv)
    {"start": start, "run": run, "session": session}[args.cmd](args)


if __name__ == "__main__":
    main()
