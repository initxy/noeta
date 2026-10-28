"""The command sandbox's vocabulary, shared by the shell and permissions
plugins and the exec env backends.

:class:`SandboxSettings` is Claude Code's ``sandbox`` settings object as
plain data (:meth:`SandboxSettings.from_json` reads it). An exec env that can
confine commands carries a :class:`CommandSandbox` as its ``sandbox``
attribute (:func:`sandbox_of`); the ``Bash`` tool asks it to
:meth:`~CommandSandbox.prepare` each command that should run confined
(:func:`runs_sandboxed`), runs the argv it gets back, and reports the hosts the
sandbox's network filter refused (:func:`violations_block`).

Nothing here starts a process.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from noeta.toolkit.permission_rules import idna, parse_rule, shell_rule_matches

__all__ = [
    "CommandSandbox",
    "PreparedCommand",
    "SandboxSettings",
    "belongs_in_sandbox",
    "domain_allowed",
    "leaves_sandbox",
    "runs_sandboxed",
    "sandbox_of",
    "violations_block",
]


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v.strip())


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _port(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        return None
    return value


@dataclass(frozen=True)
class SandboxSettings:
    """Claude Code's ``sandbox`` settings, merged across files.

    Paths are as the settings give them (absolute, ``~/...`` or relative to
    the working directory); :meth:`resolve` makes one absolute. Domains are
    host names, ``*.example.com`` for every subdomain, or ``host:port``."""

    enabled: bool = False
    #: Refuse to run a command at all when the sandbox cannot start.
    fail_if_unavailable: bool = False
    #: A command that runs sandboxed needs no approval.
    auto_allow_bash: bool = True
    #: ``dangerouslyDisableSandbox`` may take a command out (with approval).
    allow_unsandboxed: bool = True
    #: Bash permission-rule patterns that always run outside the sandbox.
    excluded_commands: tuple[str, ...] = ()
    allow_write: tuple[str, ...] = ()
    deny_write: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = ()
    allow_read: tuple[str, ...] = ()
    #: ``filesystem.disabled``: no filesystem confinement, network only.
    filesystem_disabled: bool = False
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    #: macOS: Unix socket paths a command may connect to.
    allow_unix_sockets: tuple[str, ...] = ()
    allow_all_unix_sockets: bool = False
    #: macOS: binding localhost ports (Linux commands bind inside their own
    #: network namespace either way).
    allow_local_binding: bool = False
    #: The user's own proxies, used instead of the built-in filter.
    http_proxy_port: int | None = None
    socks_proxy_port: int | None = None
    #: Linux: no fresh ``/proc`` (hosts that cannot mount one).
    weaker_nested: bool = False
    #: Linux: commands may create user namespaces of their own.
    nested_user_namespaces: bool = False
    #: ``credentials.envVars``: variables unset for sandboxed commands.
    deny_env: tuple[str, ...] = ()
    #: An absolute ``bwrap`` to use instead of the one on ``PATH``.
    bwrap_path: str | None = None
    #: Where the working directory is, for relative paths.
    base_dir: str = field(default="", compare=False)

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None, *, base_dir: str = "",
                  extra_allow_write: Iterable[str] = (), extra_deny_read: Iterable[str] = (),
                  extra_domains: Iterable[str] = ()) -> SandboxSettings:
        """The settings object (``{"enabled": true, "network": {...}, ...}``).
        ``extra_*`` are what the permission rules add: ``Edit(...)`` allow
        rules to the writable paths, ``Read(...)`` deny rules to the
        unreadable ones, ``WebFetch(domain:...)`` allow rules to the
        allowed domains. ``credentials`` entries in ``deny`` mode (and
        ``mask``, which noeta treats as ``deny``) hide the file or unset the
        variable."""
        data = data if isinstance(data, Mapping) else {}

        def table(key: str) -> Mapping[str, Any]:
            value = data.get(key)
            return value if isinstance(value, Mapping) else {}

        fs, net, creds = table("filesystem"), table("network"), table("credentials")
        cred_files = [e["path"] for e in creds.get("files") or ()
                      if isinstance(e, Mapping) and isinstance(e.get("path"), str)
                      and e.get("mode") in ("deny", "mask")]
        cred_env = [e["name"] for e in creds.get("envVars") or ()
                    if isinstance(e, Mapping) and isinstance(e.get("name"), str)
                    and e.get("mode") in ("deny", "mask")]
        bwrap = data.get("bwrapPath")
        return cls(
            enabled=_flag(data.get("enabled"), False),
            fail_if_unavailable=_flag(data.get("failIfUnavailable"), False),
            auto_allow_bash=_flag(data.get("autoAllowBashIfSandboxed"), True),
            allow_unsandboxed=_flag(data.get("allowUnsandboxedCommands"), True),
            excluded_commands=_strings(data.get("excludedCommands")),
            allow_write=_unique([*_strings(fs.get("allowWrite")), *extra_allow_write]),
            deny_write=_strings(fs.get("denyWrite")),
            deny_read=_unique([*_strings(fs.get("denyRead")), *extra_deny_read, *cred_files]),
            allow_read=_strings(fs.get("allowRead")),
            filesystem_disabled=_flag(fs.get("disabled"), False),
            allowed_domains=_unique([*_strings(net.get("allowedDomains")), *extra_domains]),
            denied_domains=_strings(net.get("deniedDomains")),
            allow_unix_sockets=_strings(net.get("allowUnixSockets")),
            allow_all_unix_sockets=_flag(net.get("allowAllUnixSockets"), False),
            allow_local_binding=_flag(net.get("allowLocalBinding"), False),
            http_proxy_port=_port(net.get("httpProxyPort")),
            socks_proxy_port=_port(net.get("socksProxyPort")),
            weaker_nested=_flag(data.get("enableWeakerNestedSandbox"), False),
            nested_user_namespaces=_flag(data.get("allowNestedUserNamespaces"), False),
            deny_env=_unique(cred_env),
            bwrap_path=bwrap if isinstance(bwrap, str) and os.path.isabs(bwrap) else None,
            base_dir=base_dir,
        )

    @classmethod
    def from_settings(cls, data: Mapping[str, Any] | None, permissions: Mapping[str, Any],
                      *, base_dir: str = "") -> SandboxSettings:
        """:meth:`from_json` with what a ``permissions`` settings object adds:
        ``Edit(...)`` allow rules and ``additionalDirectories`` (writable),
        ``Read(...)`` deny rules (unreadable), ``WebFetch(domain:...)`` allow
        rules (reachable)."""
        def contents(key: str, tool: str) -> list[str]:
            out = []
            for text in permissions.get(key) or ():
                try:
                    rule = parse_rule(text)
                except ValueError:
                    continue
                if rule.tool == tool and rule.content:
                    out.append(rule.content)
            return out

        def path(pattern: str) -> str:
            for tail in ("/**", "/*"):
                if pattern.endswith(tail):
                    return pattern[: -len(tail)] or "/"
            return pattern

        writes = [path(c) for c in contents("allow", "Edit")]
        writes += [d for d in permissions.get("additionalDirectories") or ()
                   if isinstance(d, str)]
        return cls.from_json(
            data, base_dir=base_dir, extra_allow_write=writes,
            extra_deny_read=[path(c) for c in contents("deny", "Read")],
            extra_domains=[c[len("domain:"):] for c in contents("allow", "WebFetch")
                           if c.startswith("domain:")])

    def resolve(self, path: str, base: str | None = None) -> str:
        """``path`` made absolute: ``~`` is the home directory, a relative
        path is under ``base`` (default :attr:`base_dir`)."""
        path = os.path.expanduser(path)
        if path.startswith("//"):  # a permission rule's absolute spelling
            path = path[1:]
        if not os.path.isabs(path):
            path = os.path.join(base or self.base_dir or os.getcwd(), path)
        return os.path.normpath(path)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


def _split_port(entry: str) -> tuple[str, int | None]:
    host, sep, port = entry.rpartition(":")
    if sep and port.isdigit() and host and not host.endswith(":"):
        return host.strip("[]"), int(port)
    return entry.strip("[]"), None


def _entry_matches(entry: str, host: str, port: int) -> bool:
    name, want_port = _split_port(entry.strip().lower().rstrip("."))
    if want_port is not None and want_port != port:
        return False
    if name == "*":
        return True
    if name.startswith("*."):
        suffix = idna(name[2:])
        return host.endswith("." + suffix)
    return host == idna(name)


#: Hosts that name the machine itself: reachable only when an entry names
#: them with a port (a bare host would open every local port).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})


def domain_allowed(host: str, port: int, allowed: Sequence[str],
                   denied: Sequence[str] = ()) -> bool:
    """May a sandboxed command connect to ``host:port``? A denied entry wins;
    a local address must be allowed with its port."""
    host = idna(host.strip().strip("[]").rstrip(".").lower())
    if not host:
        return False
    if any(_entry_matches(e, host, port) for e in denied):
        return False
    local = host in _LOCAL_HOSTS or host.startswith("127.")
    for entry in allowed:
        if local and _split_port(entry.strip())[1] is None:
            continue
        if _entry_matches(entry, host, port):
            return True
    return False


class PreparedCommand(Protocol):
    """One command made ready to run confined."""

    #: What to run instead of the command's own argv.
    argv: list[str]
    #: Variables to lay over the command's environment (the proxy's).
    env: dict[str, str]

    def violations(self) -> list[str]:
        """``host:port`` entries the network filter refused so far."""
        ...

    async def close(self) -> None:
        """Release what :meth:`CommandSandbox.prepare` set up."""
        ...


@runtime_checkable
class CommandSandbox(Protocol):
    """What an exec env offers when it can confine a command."""

    settings: SandboxSettings

    def unavailable(self) -> str | None:
        """Why the sandbox cannot start here, or ``None`` when it can."""
        ...

    async def prepare(self, argv: Sequence[str], *, cwd: str,
                      writable: Sequence[str] = ()) -> PreparedCommand:
        """``argv`` wrapped to run confined in ``cwd``; ``writable`` are
        extra paths it may write (a background job's log)."""
        ...


def sandbox_of(env: object) -> CommandSandbox | None:
    """``env``'s sandbox when one is switched on, else ``None``."""
    sandbox = getattr(env, "sandbox", None)
    if sandbox is None or not isinstance(sandbox, CommandSandbox):
        return None
    return sandbox if sandbox.settings.enabled else None


def runs_sandboxed(sandbox: CommandSandbox | None, command: str,
                   disable: Any = False) -> bool:
    """Does ``command`` run inside ``sandbox``? Only when it
    :func:`belongs_in_sandbox` and the sandbox can start."""
    return (sandbox is not None and belongs_in_sandbox(sandbox, command, disable)
            and sandbox.unavailable() is None)


def leaves_sandbox(sandbox: CommandSandbox | None, command: str, disable: Any) -> bool:
    """Is this the call's ``dangerouslyDisableSandbox`` taking ``command``
    out of a sandbox it would otherwise run in (what needs approval)?"""
    return (sandbox is not None and not belongs_in_sandbox(sandbox, command, disable)
            and belongs_in_sandbox(sandbox, command, False)
            and sandbox.unavailable() is None)


def belongs_in_sandbox(sandbox: CommandSandbox, command: str, disable: Any = False) -> bool:
    """Should ``command`` run sandboxed, whether or not the sandbox can
    start? Not when it is excluded, or when the call asks to leave
    (``dangerouslyDisableSandbox``) and the settings allow that."""
    settings = sandbox.settings
    if (disable is True or disable == "true") and settings.allow_unsandboxed:
        return False
    return not any(shell_rule_matches(p, command, every=False)
                   for p in _excluded(settings.excluded_commands))


def _excluded(patterns: Sequence[str]) -> list[str]:
    """``Bash(npm *)`` and ``npm *`` alike as rule content."""
    out = []
    for pattern in patterns:
        p = pattern.strip()
        if p.startswith("Bash(") and p.endswith(")"):
            p = p[5:-1]
        if p:
            out.append(p)
    return out


def violations_block(hosts: Sequence[str]) -> str:
    """The note appended to a sandboxed command's output naming the hosts
    its network filter refused."""
    if not hosts:
        return ""
    lines = "\n".join(f"- network: {h} (not in sandbox.network.allowedDomains)"
                      for h in dict.fromkeys(hosts))
    return f"<sandbox_violations>\n{lines}\n</sandbox_violations>"
