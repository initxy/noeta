"""What every ``noeta serve`` request passes before its handler runs: the
bearer token (and the tenant it picks), the ``Host`` and origin checks, and the
client that refuses a ``bypassPermissions`` mode or a ``cwd`` outside the
server's directories."""

from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Literal

import msgspec
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from noeta.core.client import ClientAPI, Input
from noeta.core.errors import UnknownTask
from noeta.core.events import DEFAULT_TENANT, Recorded
from noeta.core.loop.state import CHANGEABLE_CONFIG, CONFIG_SIGNAL
from noeta.core.store import Status, TaskRow
from noeta.toolkit.permission_modes import MODE_SIGNAL


class Refused(Exception):
    """A request asked for what this server does not allow."""


_BYPASS = "bypassPermissions"

#: The task config keys a request may set when it creates a task. The rest
#: (``permission_rules``, ``subtask_depth``, ``unattended``, ``prompts`` …)
#: are set by plugins and the server for the tasks they start; from a
#: request they would loosen the permission gates.
REQUEST_CONFIG = ("model", "effort", "max_turns", "cwd", "permission_mode", "output_schema")


class GuardedClient:
    """``client`` refusing what a request may not ask for: a task in
    ``bypassPermissions`` (unless allowed) or with a ``cwd`` outside the
    server's directory and its other ``roots``. A relative ``cwd`` is taken
    from the server's directory (not the process's) and passed on
    absolute, so every worker resolves it the same. With a ``workspace``
    (a tenant's) that directory is the only root, and a task created
    without a ``cwd`` runs in it."""

    def __init__(self, client: ClientAPI, *, allow_bypass: bool, cwd: str | None,
                 roots: Sequence[str] = (), workspace: str | None = None) -> None:
        self._client = client
        self._allow_bypass = allow_bypass
        self._fill = workspace is not None
        if workspace is not None:
            cwd, roots = workspace, ()
        self._cwd = os.path.realpath(cwd) if cwd else None
        self._roots = [r for r in (self._cwd, *(os.path.realpath(r) for r in roots)) if r]

    def _mode(self, mode: Any) -> None:
        if mode == _BYPASS and not self._allow_bypass:
            raise Refused("this server does not allow bypassPermissions "
                          "(start it with --allow-bypass-permissions)")

    def check_cwd(self, cwd: Any) -> Any:
        """``cwd`` made absolute; raises :class:`Refused` when it lies
        outside the server's directories."""
        if not cwd or self._cwd is None:
            return cwd
        real = os.path.realpath(os.path.join(self._cwd, str(cwd)))
        if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in self._roots):
            where = ", ".join(self._roots)
            if self._fill:
                raise Refused(f"cwd {cwd!r} is outside this tenant's workspace ({where})")
            raise Refused(f"cwd {cwd!r} is outside this server's directories ({where}); "
                          "start it there or add the directory with --allow-root")
        return real

    def check_config(self, config: Any, allowed: Sequence[str] = REQUEST_CONFIG) -> Any:
        """``config`` with its ``cwd`` absolute; raises :class:`Refused` for
        what a request may not ask for: a key not in ``allowed``, a
        ``bypassPermissions`` mode, a ``cwd`` outside the server's
        directories."""
        if config is None:
            return config
        if not isinstance(config, Mapping):
            raise ValueError("a task config is a JSON object")
        for key in config:
            if key not in allowed:
                raise Refused(f"a request may not set the task config key {key!r} "
                              f"(only {', '.join(allowed)})")
        self._mode(config.get("permission_mode"))
        if not config.get("cwd"):
            return config
        return {**config, "cwd": self.check_cwd(config["cwd"])}

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     queue: str = "default", dedupe_key: str | None = None,
                     history_from: str | None = None,
                     history_through: int | None = None) -> str:
        config = self.check_config(config)
        if self._fill and not (config or {}).get("cwd"):
            config = {**(config or {}), "cwd": self._cwd}
        return await self._client.create(
            agent, input, task_id=task_id, config=config, queue=queue,
            dedupe_key=dedupe_key, history_from=history_from, history_through=history_through)

    async def send(self, task_id: str, input: Input, *,
                   when: Literal["now", "next_turn"] = "next_turn",
                   dedupe_key: str | None = None) -> int:
        return await self._client.send(task_id, input, when=when, dedupe_key=dedupe_key)

    async def answer(self, task_id: str, request_id: str,
                     decision: Literal["allow", "deny"] | None = None, *,
                     always: bool = False, data: Any = None) -> int:
        if isinstance(data, Mapping):
            self._mode(data.get("mode"))
        return await self._client.answer(task_id, request_id, decision, always=always,
                                         data=data)

    async def signal(self, task_id: str, name: str, data: Any = None, *,
                     dedupe_key: str | None = None) -> int:
        if name == MODE_SIGNAL and isinstance(data, Mapping):
            self._mode(data.get("mode"))
        if name == CONFIG_SIGNAL:
            data = self.check_config(data, CHANGEABLE_CONFIG)
        return await self._client.signal(task_id, name, data, dedupe_key=dedupe_key)

    async def cancel(self, task_id: str, reason: str = "", *,
                     turn_only: bool = False) -> None:
        await self._client.cancel(task_id, reason, turn_only=turn_only)

    async def get(self, task_id: str) -> TaskRow:
        return await self._client.get(task_id)

    async def tasks(self, *, parent_id: str | None = None, status: Status | None = None,
                    limit: int = 100) -> list[TaskRow]:
        return await self._client.tasks(parent_id=parent_id, status=status, limit=limit)

    async def events(self, task_id: str, after: int = 0) -> list[Recorded]:
        return await self._client.events(task_id, after)

    def watch(self, task_id: str, after: int = 0) -> AsyncIterator[Recorded]:
        return self._client.watch(task_id, after)

    async def wait(self, task_id: str, *, timeout: float | None = None) -> TaskRow:
        return await self._client.wait(task_id, timeout=timeout)


@dataclasses.dataclass
class Scope:
    """What a request of one tenant reaches: its scoped client, that client
    guarded, and a stream-json front on it."""

    client: Any
    guarded: GuardedClient
    front: Any


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_lookup(token: str | None, tokens: Any) -> Callable[[str], str | None]:
    """A token's tenant, compared by digest (a lookup that leaks no timing
    about the tokens it holds)."""
    table: dict[str, str] = {_digest(token): DEFAULT_TENANT} if token else {}
    if isinstance(tokens, Mapping):
        table.update({_digest(t): name for t, name in tokens.items()})
    find = tokens if callable(tokens) else None

    def lookup(got: str) -> str | None:
        hit = table.get(_digest(got))
        return hit if hit is not None or find is None else find(got)

    return lookup


class _GuardedFront:
    """A stream-json front whose ``client`` is guarded."""

    def __init__(self, front: Any, client: GuardedClient) -> None:
        self._front = front
        self.client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._front, name)


#: ``Host`` names a server on a loopback address answers to.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hostname(value: str) -> str:
    """The name in a ``Host`` header or an origin's authority (no port)."""
    if value.startswith("["):
        return value[1:].partition("]")[0]
    return value.rpartition(":")[0] if value.count(":") == 1 else value


class Guard:
    """The checks before a handler: which tenant a request's token is, that
    it comes from an allowed ``Host`` and origin, and the :class:`Scope` it
    reaches (one per tenant, made on first use)."""

    def __init__(self, client: Any, serving: Any, *, token: str | None, tokens: Any,
                 auth: bool, allowed_hosts: Sequence[str] | None, origins: Sequence[str],
                 allow_bypass: bool, cwd: str | None, roots: Sequence[str],
                 workspaces: Mapping[str, str] | None) -> None:
        self._client = client
        self._serving = serving
        self._auth = auth
        self._tenant_of = _token_lookup(token, tokens)
        self._allow_bypass = allow_bypass
        self._cwd = cwd
        self._roots = roots
        self._workspaces = workspaces
        self._scopes: dict[str, Scope] = {}
        self._hosts = None if allowed_hosts is None else {h.lower() for h in allowed_hosts}
        self._origins = {o.rstrip("/").lower() for o in origins}

    def scope_for(self, tenant: str) -> Scope:
        found = self._scopes.get(tenant)
        if found is None:
            client = self._client
            scoped = client.scoped(tenant) if hasattr(client, "scoped") else client
            guarded = GuardedClient(scoped, allow_bypass=self._allow_bypass, cwd=self._cwd,
                                    roots=self._roots,
                                    workspace=(self._workspaces or {}).get(tenant))
            found = self._scopes[tenant] = Scope(scoped, guarded,
                                                 _GuardedFront(self._serving, guarded))
        return found

    def tenant(self, headers: Any, query: Any) -> str | None:
        """The request's tenant; ``None`` when its token is missing or wrong."""
        if not self._auth:
            return DEFAULT_TENANT
        got = headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or query.get("token", "")
        return self._tenant_of(got) if got else None

    def same_site(self, headers: Any) -> bool:
        """Whether the request comes from an allowed ``Host`` and, when a
        browser sent it, from this server's own page or an allowed one."""
        host = headers.get("host", "")
        if self._hosts is not None and _hostname(host).lower() not in self._hosts:
            return False
        origin = headers.get("origin")
        if origin is None:
            return True
        origin = origin.rstrip("/").lower()
        return origin in self._origins or origin.partition("://")[2] == host.lower()

    def __call__(self, handler: Any) -> Any:
        """``handler`` behind the checks, with ``request.state.scope`` set and
        its errors answered with their status codes."""
        async def wrapped(request: Request) -> Response:
            if not self.same_site(request.headers):
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
            who = self.tenant(request.headers, request.query_params)
            if who is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            request.state.scope = self.scope_for(who)
            try:
                result: Response = await handler(request)
                return result
            except UnknownTask as exc:
                return JSONResponse({"error": f"unknown task {exc}"}, status_code=404)
            except Refused as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except (msgspec.ValidationError, ValueError, KeyError, IndexError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped


def scope(request: Request) -> Scope:
    """The :class:`Scope` the guard set on ``request``."""
    found: Scope = request.state.scope
    return found
