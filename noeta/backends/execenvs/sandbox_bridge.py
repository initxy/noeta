"""Runs first inside a Linux sandbox (``python -I sandbox_bridge.py ...``),
standard library only.

The sandbox has a network namespace of its own with only a loopback, so the
command reaches the outside solely through the proxies the host serves on
Unix sockets bound into the sandbox. This script listens on loopback TCP
ports inside the namespace (the ones the proxy variables name) and relays each
connection to those sockets, then starts the command with a seccomp filter
that refuses to create Unix sockets, alone or in pairs (so the command cannot reach a host
daemon's socket, such as Docker's) and new user namespaces (which would let
it undo the mounts), unless the settings allow them, and always refuses
tracing or reading into another process. The relay itself is not dumpable,
so the command cannot reach into it (and its sockets) by ``/proc`` either.
It exits with the command's status.

Usage: ``sandbox_bridge.py [--http SOCK PORT] [--socks SOCK PORT]
[--allow-unix] [--allow-userns] -- ARGV...``
"""

from __future__ import annotations

import ctypes
import os
import platform
import signal
import socket
import struct
import sys
import threading

_CHUNK = 64 * 1024


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _listen(port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(128)
    return server


def _serve(server: socket.socket, path: str) -> None:
    def accept() -> None:
        while True:
            try:
                client, _ = server.accept()
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(path)
            except OSError:
                try:
                    client.close()
                except (OSError, UnboundLocalError):
                    pass
                continue
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=_relay, args=(a, b), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()


# ---------------------------------------------------------------- seccomp

_ARCHES = {
    # machine: (AUDIT_ARCH, socket, unshare, clone, clone3, x32 bit,
    # (ptrace, process_vm_readv, process_vm_writev, pidfd_getfd))
    "x86_64": (0xC000003E, 41, 272, 56, 435, True, (101, 310, 311, 438)),
    "aarch64": (0xC00000B7, 198, 97, 220, 435, False, (117, 270, 271, 438)),
}
#: socketpair: a connected pair, but a datagram one sends to any Unix
#: socket by address (``sendto``), such as the host's journald.
_SOCKETPAIR = {"x86_64": 53, "aarch64": 199}
# io_uring_setup, io_uring_enter, io_uring_register: the same numbers on
# every architecture (added after the tables were unified).
_IO_URING = (425, 426, 427)
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_EPERM, _ENOSYS = 1, 38
_CLONE_NEWUSER = 0x10000000


def filter_unsupported(allow_unix: bool, allow_userns: bool) -> str | None:
    """Why the filter cannot refuse what the settings do not allow on this
    machine's architecture, or ``None``."""
    machine = platform.machine()
    if machine in _ARCHES or (allow_unix and allow_userns):
        # Unknown and allowing both: only the relay's being non-dumpable
        # keeps the command out of it.
        return None
    return (f"the sandbox's system-call filter (refusing Unix sockets and new user "
            f"namespaces) does not support the {machine or 'unknown'} architecture")


def _errno(code: int) -> int:
    return 0x00050000 | code


def _filter(allow_unix: bool, allow_userns: bool) -> bytes | None:
    """The BPF program, or ``None`` when this machine's architecture is not
    one it knows."""
    arch = _ARCHES.get(platform.machine())
    if arch is None:
        return None
    audit, nr_socket, nr_unshare, nr_clone, nr_clone3, x32, refused = arch
    ld, jeq, jset, ret = 0x20, 0x15, 0x45, 0x06
    # A call made through another ABI (i386's ``int 0x80`` on x86_64) has
    # other numbers the checks below would miss: it kills the process.
    prog: list[tuple[int, int, int, int]] = [
        (ld, 0, 0, 4), (jeq, 1, 0, audit), (ret, 0, 0, _RET_KILL_PROCESS), (ld, 0, 0, 0)]
    if x32:
        # x32 calls share the arch but set this bit in the number.
        prog += [(jset, 0, 1, 0x40000000), (ret, 0, 0, _errno(_ENOSYS))]

    def flag_block(nr: int, test: tuple[int, int, int, int]) -> None:
        # nr matches: load arg0, refuse when the test holds, else allow.
        prog.extend([(jeq, 0, 4, nr), (ld, 0, 0, 16), test,
                     (ret, 0, 0, _errno(_EPERM)), (ret, 0, 0, _RET_ALLOW)])

    for nr in refused:
        prog += [(jeq, 0, 1, nr), (ret, 0, 0, _errno(_EPERM))]
    if not allow_unix:
        # io_uring creates sockets without socket(2) on recent kernels.
        for nr in _IO_URING:
            prog += [(jeq, 0, 1, nr), (ret, 0, 0, _errno(_EPERM))]
        flag_block(nr_socket, (jeq, 0, 1, socket.AF_UNIX))
        flag_block(_SOCKETPAIR[platform.machine()], (jeq, 0, 1, socket.AF_UNIX))
    if not allow_userns:
        flag_block(nr_unshare, (jset, 0, 1, _CLONE_NEWUSER))
        flag_block(nr_clone, (jset, 0, 1, _CLONE_NEWUSER))
        # clone3's flags sit in memory a filter cannot read: say it is
        # missing, and the C library falls back to clone.
        prog += [(jeq, 0, 1, nr_clone3), (ret, 0, 0, _errno(_ENOSYS))]
    prog.append((ret, 0, 0, _RET_ALLOW))
    return b"".join(struct.pack("HBBI", *ins) for ins in prog)


def _install(program: bytes) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    buf = ctypes.create_string_buffer(program)

    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = Prog(len(program) // 8, ctypes.addressof(buf))
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0:  # PR_SET_SECCOMP, FILTER
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP failed")


def main(args: list[str]) -> int:
    allow_unix = allow_userns = False
    relays: list[tuple[str, int]] = []
    while args and args[0] != "--":
        opt = args.pop(0)
        if opt in ("--http", "--socks"):
            path, port = args.pop(0), int(args.pop(0))
            relays.append((path, port))
        elif opt == "--allow-unix":
            allow_unix = True
        elif opt == "--allow-userns":
            allow_userns = True
        else:
            print(f"sandbox: unknown option {opt}", file=sys.stderr)
            return 2
    argv = args[1:]
    if not argv:
        print("sandbox: no command", file=sys.stderr)
        return 2
    # Listening before the command starts, relaying (threads) only after
    # the fork: the child is forked from a single-threaded process.
    servers = [(_listen(port), path) for path, port in relays]
    # Not dumpable: a command running as the same user can neither trace
    # the relay nor open its /proc files (fds, memory) whatever
    # kernel.yama.ptrace_scope says. The command's own exec resets it.
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        print("sandbox: cannot make the relay non-dumpable", file=sys.stderr)
        return 2
    program = _filter(allow_unix, allow_userns)
    pid = os.fork()
    if pid == 0:
        try:
            if program is not None:
                _install(program)
            os.execvp(argv[0], argv)
        except OSError as exc:
            print(f"sandbox: cannot start {argv[0]}: {exc}", file=sys.stderr)
        os._exit(127)
    for server, path in servers:
        _serve(server, path)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda s, _f: os.kill(pid, s))
    while True:
        try:
            _, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
