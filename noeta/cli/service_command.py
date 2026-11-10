"""``noeta service``: run ``noeta serve`` as this user's service, started at
login (or boot, with lingering) and restarted when it crashes.

    noeta [--model M ...] service install [serve flags]   write the service and start it
    noeta service uninstall                               stop it and remove it
    noeta service start | stop | restart
    noeta service status                                  its state and the addresses to open
    noeta service logs [--no-follow]                      its output

On Linux a systemd user unit (``~/.config/systemd/user/noeta.service``, its
environment in ``noeta.env`` beside it, owner-only); on macOS a launchd
LaunchAgent (``~/Library/LaunchAgents/dev.noeta.serve.plist``, owner-only,
output in ``~/.noeta/logs/serve.log``). The service runs ``noeta serve`` with
the flags given to ``install`` in the home directory, with the provider,
``NOETA_*`` and proxy variables and ``PATH`` / ``HOME`` of the shell that
installed it: changing them takes ``install`` again, which replaces the
service."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from noeta.cli import home_dir
from noeta.toolkit.local_env import env_refs

UNIT = "noeta.service"
LABEL = "dev.noeta.serve"
#: The prefixes of the variables the service gets from the installing shell.
ENV_PREFIXES = ("NOETA_", "ANTHROPIC_", "OPENAI_", "DEEPSEEK_")
_PROXIES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
#: The other variables it gets: the proxies (both spellings), ``PATH`` and ``HOME``.
ENV_NAMES = (*_PROXIES, *(p.lower() for p in _PROXIES), "PATH", "HOME")
#: The flags whose value is a path, made absolute (the service runs in the home directory).
PATH_FLAGS = ("cwd", "web")

NO_MANAGER = ("noeta service: no systemd user instance or launchd here to run a service; "
              "run noeta serve in tmux (tmux new -d -s noeta 'noeta serve') instead")
NOT_INSTALLED = "noeta service: not installed (noeta service install [serve flags])"

Manager = Literal["systemd", "launchd"]
#: Adds the ``noeta serve`` flags to a parser (``noeta.cli.main``'s, shared).
AddArguments = Callable[[argparse.ArgumentParser], None]


class Runner:
    """Runs ``systemctl``, ``launchctl`` and the rest; the one place a test
    replaces. ``capture=False`` hands the terminal over (``logs``)."""

    def __call__(self, argv: Sequence[str], *,
                 capture: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(list(argv), capture_output=capture, text=True, check=False)
        except OSError as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", exc.strerror or str(exc))


def add_parser(sub: Any, serve_arguments: AddArguments) -> None:
    sv = sub.add_parser("service", help="run noeta serve as this user's service "
                                        "(systemd user unit, launchd LaunchAgent)")
    actions = sv.add_subparsers(dest="service_action", required=True)
    install = actions.add_parser(
        "install", help="write the service (noeta serve with these flags) and start it; "
                        "again to replace it")
    serve_arguments(install)
    actions.add_parser("uninstall", help="stop the service and remove it")
    for name, what in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
        actions.add_parser(name, help=f"{what} the service")
    actions.add_parser("status", help="the service's state and the addresses to open")
    logs = actions.add_parser("logs", help="the service's output (the last 200 lines, then "
                                           "what follows)")
    logs.add_argument("--no-follow", action="store_true", help="print the last lines and stop")


def service_env(environ: Mapping[str, str], named: Collection[str] = ()) -> dict[str, str]:
    """The variables of ``environ`` the service runs with: noeta's and the
    providers' own, ``PATH``, ``HOME``, the proxy ones, and ``named`` (those
    noeta's ``config.toml`` refers to as ``{env:NAME}``)."""
    return {k: v for k, v in sorted(environ.items())
            if k.startswith(ENV_PREFIXES) or k in ENV_NAMES or k in named}


def config_env_refs() -> set[str]:
    """The variables noeta's ``config.toml`` names in ``{env:NAME}``
    references (its keys and headers); none when it is missing or invalid,
    which ``noeta serve`` itself reports."""
    path = home_dir.HOME / "config.toml"
    try:
        return env_refs(tomllib.loads(path.read_text("utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        return set()


def flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[str]:
    """The command-line flags of ``parser`` that ``args`` sets to other than
    their defaults, as words (paths made absolute)."""
    out: list[str] = []
    for action in parser._actions:
        if not action.option_strings or action.dest == "help":
            continue
        value = getattr(args, action.dest, action.default)
        if value is None or value == action.default or value is argparse.SUPPRESS:
            continue
        name = max(action.option_strings, key=len)
        if action.nargs == 0:
            out.append(name)
            continue
        for item in value if isinstance(value, list) else [value]:
            text = str(item)
            out += [name, os.path.abspath(text) if action.dest in PATH_FLAGS else text]
    return out


def noeta_command() -> list[str]:
    """How to start this noeta: the ``noeta`` script that runs now, else
    this Python with ``-m``."""
    script = sys.argv[0] if sys.argv else ""
    if os.path.basename(script) == "noeta" and os.path.isfile(script):
        return [os.path.abspath(script)]
    return [sys.executable, "-m", "noeta.cli.main"]


_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _systemd_word(word: str) -> str:
    """``word`` as one ExecStart argument: quoted unless plain, with
    systemd's specifiers (``%``) and variables (``$``) taken literally."""
    quoted = word if _SAFE.fullmatch(word) else \
        '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return quoted.replace("%", "%%").replace("$", "$$")


def systemd_unit(argv: Sequence[str], *, home: str, env_file: str) -> str:
    """The user unit running ``argv``."""
    return (
        "# Written by noeta service install; run it again to change this unit.\n"
        "[Unit]\n"
        "Description=noeta serve (the HTTP API and the web app)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(_systemd_word(w) for w in argv)}\n"
        f"WorkingDirectory={_systemd_word(home)}\n"
        f"EnvironmentFile={_systemd_word(env_file)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n")


def systemd_env_file(env: Mapping[str, str]) -> str:
    """``env`` as a systemd ``EnvironmentFile`` (a value spanning lines is
    left out: the format has none)."""
    lines = ["# Written by noeta service install: the installing shell's variables."]
    for key, value in env.items():
        if "\n" in value:
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def launchd_plist(argv: Sequence[str], *, home: str, env: Mapping[str, str],
                  log: str) -> bytes:
    """The LaunchAgent running ``argv``."""
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": list(argv), "WorkingDirectory": home,
        "EnvironmentVariables": dict(env), "RunAtLoad": True, "KeepAlive": True,
        "StandardOutPath": log, "StandardErrorPath": log})


def _unit_argv(text: str) -> list[str]:
    """The words of a unit's ``ExecStart`` (as :func:`_systemd_word` wrote them)."""
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return [w.replace("%%", "%").replace("$$", "$")
                    for w in shlex.split(line.removeprefix("ExecStart="))]
    return []


def _write(path: Path, data: str | bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = path.with_name(f".{path.name}-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        fresh.unlink()
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)
    os.replace(fresh, path)


@dataclass
class Service:
    """The service on this machine, as its manager keeps it."""

    manager: Manager
    run: Runner
    #: ``~/.config/systemd/user`` or ``~/Library/LaunchAgents``.
    directory: Path
    home: str
    uid: int
    user: str

    @property
    def path(self) -> Path:
        return self.directory / (UNIT if self.manager == "systemd" else f"{LABEL}.plist")

    @property
    def env_path(self) -> Path:
        return self.directory / "noeta.env"

    @property
    def log_path(self) -> Path:
        return home_dir.HOME / "logs" / "serve.log"

    @property
    def _target(self) -> str:
        return f"gui/{self.uid}/{LABEL}"

    def installed(self) -> bool:
        return self.path.exists()

    def argv(self) -> list[str]:
        """The command the installed service runs."""
        if self.manager == "systemd":
            return _unit_argv(self.path.read_text("utf-8"))
        found = plistlib.loads(self.path.read_bytes()).get("ProgramArguments")
        return [str(w) for w in found] if isinstance(found, list) else []

    def env(self) -> dict[str, str]:
        """The variables the installed service runs with."""
        if self.manager == "launchd":
            found = plistlib.loads(self.path.read_bytes()).get("EnvironmentVariables")
            return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}
        out: dict[str, str] = {}
        with contextlib.suppress(OSError):
            for line in self.env_path.read_text("utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and not line.startswith("#"):
                    out[key] = shlex.split(value)[0] if value else ""
        return out

    def _ctl(self, *words: str) -> subprocess.CompletedProcess[str]:
        if self.manager == "systemd":
            return self.run(["systemctl", "--user", *words])
        return self.run(["launchctl", *words])

    def install(self, argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
        """Write the service and (re)start it; the problems, if any."""
        if self.manager == "systemd":
            _write(self.env_path, systemd_env_file(env), 0o600)
            _write(self.path, systemd_unit(argv, home=self.home, env_file=str(self.env_path)),
                   0o644)
            # restart: a running one runs the new unit, a stopped one starts.
            return self._steps([("daemon-reload",), ("enable", UNIT), ("restart", UNIT)])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ctl("bootout", self._target)  # a previous one, if loaded
        _write(self.path, launchd_plist(argv, home=self.home, env=env, log=str(self.log_path)),
               0o600)
        return self._steps([("bootstrap", f"gui/{self.uid}", str(self.path))])

    def _steps(self, steps: Sequence[Sequence[str]]) -> list[str]:
        problems = []
        for step in steps:
            done = self._ctl(*step)
            if done.returncode != 0:
                problems.append(_problem(done))
        return problems

    def uninstall(self) -> list[str]:
        if self.manager == "systemd":
            problems = self._steps([("disable", "--now", UNIT)])
            for path in (self.path, self.env_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            return problems + self._steps([("daemon-reload",)])
        self._ctl("bootout", self._target)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        return []

    def control(self, action: str) -> list[str]:
        """``start``, ``stop`` or ``restart``."""
        if self.manager == "systemd":
            return self._steps([(action, UNIT)])
        if action == "stop":
            return self._steps([("bootout", self._target)])
        if action == "start":
            loaded = self._ctl("print", self._target).returncode == 0
            return [] if loaded else self._steps([("bootstrap", f"gui/{self.uid}",
                                                   str(self.path))])
        return self._steps([("kickstart", "-k", self._target)])

    def state(self) -> str:
        """``active``, ``inactive``, ``failed`` … as the manager says."""
        if self.manager == "systemd":
            return self._ctl("is-active", UNIT).stdout.strip() or "unknown"
        done = self._ctl("print", self._target)
        if done.returncode != 0:
            return "not loaded"
        found = re.search(r"^\s*state = (\S+)", done.stdout, re.MULTILINE)
        return found.group(1) if found else "loaded"

    def logs(self, follow: bool) -> int:
        if self.manager == "systemd":
            argv = ["journalctl", "--user", "-u", UNIT, "-n", "200", "--no-pager"]
        else:
            argv = ["tail", "-n", "200", str(self.log_path)]
        return self.run([*argv, *(["-f"] if follow else [])], capture=False).returncode

    def linger_off(self) -> bool:
        """Whether this user's systemd instance stops at logout (Linux)."""
        if self.manager != "systemd":
            return False
        done = self.run(["loginctl", "show-user", self.user, "--property=Linger", "--value"])
        return done.returncode == 0 and done.stdout.strip() != "yes"


def _problem(done: subprocess.CompletedProcess[str]) -> str:
    said = (done.stderr or done.stdout or "").strip().splitlines()
    return f"{shlex.join(done.args)}: {said[-1] if said else f'exit {done.returncode}'}"


def detect(run: Runner | None = None, *, platform: str = sys.platform,
           home: str | None = None) -> Service | None:
    """The service manager of this user, or ``None`` when there is none to use."""
    run = run or Runner()
    home = home or str(Path.home())
    user = getpass.getuser()
    if platform == "darwin" and shutil.which("launchctl"):
        return Service("launchd", run, Path(home) / "Library" / "LaunchAgents", home,
                       os.getuid(), user)
    if platform.startswith("linux") and shutil.which("systemctl") \
            and run(["systemctl", "--user", "show-environment"]).returncode == 0:
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return Service("systemd", run, Path(config) / "systemd" / "user", home,
                       os.getuid(), user)
    return None


def _addresses(service: Service, serve_arguments: AddArguments) -> list[str]:
    """The addresses to open the installed service at, from its flags and token."""
    from noeta.cli.serve_address import addresses, machine_names
    from noeta.cli.server_token import server_token
    from noeta.host.http import LOOPBACK_HOSTS

    argv = service.argv()
    if "serve" not in argv:
        return []
    parser = argparse.ArgumentParser(add_help=False)
    serve_arguments(parser)
    args, _ = parser.parse_known_args(argv[argv.index("serve") + 1:])
    token = None if args.no_token else (
        service.env().get("NOETA_SERVER_TOKEN") or server_token())
    machine = [] if args.host in LOOPBACK_HOSTS else machine_names()
    return addresses(args.host, args.port, token, args.allow_host, machine)


def _report(problems: Sequence[str]) -> int:
    for problem in problems:
        print(f"noeta service: {problem}", file=sys.stderr)
    return 1 if problems else 0


def run(args: argparse.Namespace, *, serve_arguments: AddArguments,
        common: AddArguments, service: Service | None = None,
        environ: Mapping[str, str] | None = None) -> int:
    service = service or detect()
    if service is None:
        print(NO_MANAGER, file=sys.stderr)
        return 1
    action = args.service_action
    if action == "install":
        return _install(service, args, serve_arguments, common,
                        os.environ if environ is None else environ)
    if not service.installed():
        print(NOT_INSTALLED, file=sys.stderr)
        return 1
    if action == "uninstall":
        code = _report(service.uninstall())
        print(f"noeta service: removed {service.path}")
        return code
    if action in ("start", "stop", "restart"):
        code = _report(service.control(action))
        if code == 0:
            print(f"noeta service: {action} done; now {service.state()}")
        return code
    if action == "logs":
        return service.logs(follow=not args.no_follow)
    print(f"noeta service: {service.state()} ({service.path})")
    for url in _addresses(service, serve_arguments):
        print(f"noeta: at {url}")
    return 0


def _install(service: Service, args: argparse.Namespace, serve_arguments: AddArguments,
             common: AddArguments, environ: Mapping[str, str]) -> int:
    from noeta.cli.server_token import serve_token

    top = argparse.ArgumentParser(add_help=False)
    common(top)
    serving = argparse.ArgumentParser(add_help=False)
    serve_arguments(serving)
    argv = [*noeta_command(), *flags(top, args), "serve", *flags(serving, args)]
    named = config_env_refs()
    env = service_env(environ, named)
    if missing := sorted(n for n in named if n not in environ):
        print(f"noeta service: config.toml refers to {', '.join(missing)}, which this shell "
              "does not set; set it and install again, or the service cannot use it",
              file=sys.stderr)
    if not args.no_token and "NOETA_SERVER_TOKEN" not in env:
        serve_token()  # made now, so status can show it before the service first starts
    replacing = service.installed()
    if problems := service.install(argv, env):
        return _report(problems)
    print(f"noeta service: {'replaced' if replacing else 'installed'} {service.path} "
          f"running: {shlex.join(argv)}")
    print(f"noeta service: {service.state()}; it starts at login and again after a crash "
          "(noeta service status | logs | stop | uninstall)")
    if service.linger_off():
        print(f"noeta service: lingering is off for {service.user}, so the service stops "
              f"when you log out; to keep it running: loginctl enable-linger {service.user}")
    names = ", ".join(sorted(env)) or "none"
    print(f"noeta service: its environment ({names}) is this shell's now; after changing "
          "a key or a variable, install again")
    for url in _addresses(service, serve_arguments):
        print(f"noeta: at {url}")
    return 0


def restart_if_installed(service: Service | None = None) -> bool | None:
    """Restart the installed service (``noeta update``): ``None`` when there
    is none, else whether it restarted."""
    with contextlib.suppress(OSError):
        service = service or detect()
    if service is None or not service.installed():
        return None
    return not service.control("restart")
