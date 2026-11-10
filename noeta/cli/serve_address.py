"""The ``Host`` names ``noeta serve`` answers and the addresses it prints:
on a loopback bind the loopback names; on any other bind also this machine's
own names and addresses; ``--allow-host`` names on either."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Sequence

from noeta.host.http import LOOPBACK_HOSTS
from noeta.toolkit.sandbox import own_addresses

#: Binds that listen on every address of the machine.
WILDCARD_HOSTS = ("0.0.0.0", "::", "")


def machine_names() -> list[str]:
    """This machine's hostname, its fully qualified name and its addresses
    (the routing table's and those its name resolves to). Name lookups may
    wait on DNS: call it off the event loop."""
    hostname = socket.gethostname()
    names = [hostname]
    with contextlib.suppress(OSError):
        names.append(socket.getfqdn())
    names += own_addresses()
    with contextlib.suppress(OSError):
        names += [str(info[4][0]) for info in socket.getaddrinfo(hostname, None)]
    return [n for n in dict.fromkeys(names) if n and not _loopback(n) and "%" not in n]


def _loopback(name: str) -> bool:
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return name in LOOPBACK_HOSTS


def allowed_hosts(host: str, allow_host: Sequence[str], machine: Sequence[str]) -> list[str]:
    """The ``Host`` names a server bound to ``host`` answers: the loopback
    names and ``allow_host``, and on a bind that is not loopback also
    ``machine`` (:func:`machine_names`)."""
    own = [] if host in LOOPBACK_HOSTS else list(machine)
    return list(dict.fromkeys([*LOOPBACK_HOSTS, *own, *allow_host]))


def _url(name: str, port: int) -> str:
    return f"http://[{name}]:{port}/" if ":" in name else f"http://{name}:{port}/"


def addresses(host: str, port: int, token: str | None, allow_host: Sequence[str],
              machine: Sequence[str]) -> list[str]:
    """The addresses to open, ``#token=`` added: the bind's own on a
    loopback or a single address; on every address, ``machine``'s names
    and loopback; ``allow_host`` names either way."""
    if host in WILDCARD_HOSTS:
        names = [*machine, *allow_host, "127.0.0.1"]
    else:
        names = [host, *allow_host]
    fragment = f"#token={token}" if token else ""
    return [_url(n, port) + fragment for n in dict.fromkeys(names)]
