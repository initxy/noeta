"""The command sandbox's vocabulary, shared by the shell and permissions
plugins and the exec env backends.

:class:`SandboxSettings` is Claude Code's ``sandbox`` settings object as
plain data (:meth:`SandboxSettings.from_json` reads it). An exec env that can
confine commands carries a :class:`CommandSandbox` as its ``sandbox``
attribute (:func:`sandbox_of`); the ``Bash`` tool asks it to
:meth:`~CommandSandbox.prepare` each command that should run confined
(:func:`runs_sandboxed`), runs the argv it gets back, and reports the hosts the
sandbox's network filter refused (:func:`violations_block`). Nothing here
starts a process; :func:`resolve_allowed` looks a name up.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from noeta.toolkit.permission_rules import idna, parse_rule, shell_rule_matches

__all__ = [
    "DEFAULT_DENY_READ",
    "CommandSandbox",
    "PreparedCommand",
    "SandboxSettings",
    "address_refusal",
    "belongs_in_sandbox",
    "domain_allowed",
    "leaves_sandbox",
    "resolve_allowed",
    "runs_sandboxed",
    "sandbox_of",
    "violations_block",
]

#: Credential stores in the home directory a confined command never reads
#: unless ``allowRead`` names them.
DEFAULT_DENY_READ = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.config/gcloud", "~/.kube",
    "~/.docker/config.json", "~/.config/gh", "~/.netrc", "~/.git-credentials",
    "~/.pypirc", "~/.npmrc", "~/.cargo/credentials", "~/.cargo/credentials.toml",
    "~/.pgpass", "~/.password-store",
)


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


def _names_this_machine(host: str) -> bool:
    """Whether ``host`` names the machine itself — ``localhost`` or a
    loopback or unspecified address in any spelling (``[0:0::1]``,
    ``::ffff:127.0.0.1``): judged by the parsed address, never the text."""
    if host == "localhost":
        return True
    ip = _address(host)
    return ip is not None and (ip.is_loopback or ip.is_unspecified)


def domain_allowed(host: str, port: int, allowed: Sequence[str],
                   denied: Sequence[str] = ()) -> bool:
    """May a sandboxed command connect to ``host:port``? A denied entry wins;
    a local address must be allowed with its port (a bare host, or ``*``,
    would open every local port)."""
    host = idna(host.strip().strip("[]").rstrip(".").lower())
    if not host:
        return False
    if any(_entry_matches(e, host, port) for e in denied):
        return False
    local = _names_this_machine(host)
    for entry in allowed:
        if local and _split_port(entry.strip())[1] is None:
            continue
        if _entry_matches(entry, host, port):
            return True
    return False


#: Addresses an allowed name may not resolve to (Claude Code's list): a
#: rebinding or a misconfigured record would reach the machine itself, the
#: link or a cloud's metadata service.
_BLOCKED_RANGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("a loopback address", ("127.0.0.0/8", "::1/128")),
    ("an unspecified address", ("0.0.0.0/8", "::/128")),
    ("a link-local address", ("169.254.0.0/16", "fe80::/10")),
    ("a multicast address", ("224.0.0.0/4", "ff00::/8")),
    ("the broadcast address", ("255.255.255.255/32",)),
    ("a cloud metadata address", (
        "100.100.100.200/32", "168.63.129.16/32", "192.0.0.192/32", "fd00:ec2::/32",
        "fd20:ce::254/128", "fd00:c1::a9fe:a9fe/128", "fd00:42::42/128",
        "fd00:a9fe:a9fe::1/128", "fd00:100::100:200/128")),
)
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _address(value: str) -> _Address | None:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if 1 < int(ip) < 1 << 32:  # IPv4-compatible (``::127.0.0.1``)
            return ipaddress.IPv4Address(int(ip))
    return ip


def _listed(ip: _Address, port: int, entries: Sequence[str]) -> bool:
    """Whether an IP or CIDR entry (``host:port`` or bare) covers ``ip``."""
    for entry in entries:
        name, want_port = _split_port(entry.strip())
        if want_port is not None and want_port != port:
            continue
        try:
            if ip in ipaddress.ip_network(name, strict=False):
                return True
        except ValueError:
            continue
    return False


def address_refusal(host: str, address: str, port: int, allowed: Sequence[str],
                    denied: Sequence[str] = (), own: Sequence[str] = ()) -> str | None:
    """Why a connection for ``host:port`` may not go to ``address`` (what
    the name resolved to), or ``None`` when it may. An IP or CIDR entry in
    the allowed list lets an address through; a loopback name may reach
    only loopback; ``own`` are this machine's addresses."""
    ip = _address(address)
    if ip is None:
        return "an unparsable address"
    if _listed(ip, port, denied):
        return "a deny-listed address"
    if _listed(ip, port, allowed):
        return None
    name = host.strip().strip("[]").rstrip(".").lower()
    literal = _address(name)
    if name == "localhost" or (literal is not None and literal.is_loopback):
        return None if ip.is_loopback else "a non-loopback address"
    for reason, ranges in _BLOCKED_RANGES:
        if any(ip in ipaddress.ip_network(r) for r in ranges):
            return reason
    if any(_address(a) == ip for a in own):
        return "one of this host's addresses"
    return None


def _own_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


async def resolve_allowed(host: str, port: int, allowed: Sequence[str],
                          denied: Sequence[str] = ()) -> str:
    """The address to connect to for ``host:port`` — the first it resolves
    to that :func:`address_refusal` lets through, so the connection goes
    where it was checked (no second lookup to rebind). Raises
    :class:`PermissionError` naming why when none is."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    own = _own_addresses()
    reasons: list[str] = []
    for info in infos:
        address = str(info[4][0])
        reason = address_refusal(host, address, port, allowed, denied, own)
        if reason is None:
            return address
        reasons.append(f"{address} ({reason})")
    raise PermissionError(f"{host} resolves to {', '.join(reasons) or 'nothing'}")


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


def sandbox_settings(value: SandboxSettings | Mapping[str, Any] | None, *,
                     base_dir: str = "") -> SandboxSettings | None:
    """``value`` as settings: kept when it already is, read with
    :meth:`SandboxSettings.from_json` (relative paths against ``base_dir``)
    when it is a mapping."""
    if value is None or isinstance(value, SandboxSettings):
        return value
    return SandboxSettings.from_json(value, base_dir=base_dir)


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
