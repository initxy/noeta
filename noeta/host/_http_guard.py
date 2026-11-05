"""What every ``noeta serve`` request passes before its handler runs: the
bearer token, the ``Host`` and origin checks, and the client that refuses a
``bypassPermissions`` mode or a ``cwd`` outside the server's directories."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal

import msgspec
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from noeta.core.client import CONTINUED_AS, CONTINUED_AS_KEY, ClientAPI, Input
from noeta.core.errors import UnknownTask
from noeta.core.events import Recorded
from noeta.core.loop.state import CHANGEABLE_CONFIG, CONFIG_SIGNAL, MODEL_ROUTE
from noeta.core.store import Status, TaskRow
from noeta.host._errors import error_text
from noeta.host.rewind import RESTORE_SIGNAL
from noeta.toolkit.hook_callbacks import CLIENT_HOOKS_KEY
from noeta.toolkit.permission_modes import MODE_SIGNAL
from noeta.toolkit.signals import ARCHIVE_SIGNAL, RENAME_SIGNAL, RESUMED_SIGNAL
from noeta.toolkit.structured_output import SCHEMA_KEY, answer_schema_problem
from noeta.toolkit.usd_budget import BUDGET_KEY


def token_digest(token: str) -> str:
    """What is kept of a bearer token: its SHA-256, in hex."""
    return hashlib.sha256(token.encode()).hexdigest()


class Refused(Exception):
    """A request asked for what this server does not allow."""


_BYPASS = "bypassPermissions"

#: The task config keys a request may set when it creates a task. The rest
#: (``permission_rules``, ``subtask_depth``, ``unattended``, ``prompts`` …)
#: are set by plugins and the server for the tasks they start; from a
#: request they would loosen the permission gates.
REQUEST_CONFIG = ("model", "effort", "max_turns", "cwd", "permission_mode", "output_schema",
                  "timezone", "interactive", "permission_prompt_tool", "ultracode",
                  BUDGET_KEY, CONTINUED_AS_KEY)

#: The signals a request may post: the ones front ends send. The others
#: (a background job's end, a sub-agent's idle subscription …) are posted
#: by plugins inside the server, which trust what they say.
REQUEST_SIGNALS = (MODE_SIGNAL, CONFIG_SIGNAL, RESUMED_SIGNAL, RESTORE_SIGNAL,
                   RENAME_SIGNAL, ARCHIVE_SIGNAL)

#: The config keys a request's config signal may change: a model's route
#: (its provider, its limits) is the server's to record, never a client's.
REQUEST_CHANGEABLE = tuple(k for k in CHANGEABLE_CONFIG if k != MODEL_ROUTE)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class GuardedClient:
    """``client`` refusing what a request may not ask for: a task in
    ``bypassPermissions`` (unless allowed) or with a ``cwd`` outside the
    server's directory and its other ``roots``. A relative ``cwd`` is taken
    from the server's directory (not the process's) and passed on
    absolute, so every worker resolves it the same. A task created without a
    ``cwd`` runs in the server's directory (recorded, so every worker runs
    it there)."""

    def __init__(self, client: ClientAPI, *, allow_bypass: bool, cwd: str | None,
                 roots: Sequence[str] = ()) -> None:
        self._client = client
        self._allow_bypass = allow_bypass
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
        if not any(_within(real, r) for r in self._roots):
            where = ", ".join(self._roots)
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
        if config.get(CONTINUED_AS_KEY, "fork") not in CONTINUED_AS:
            raise ValueError(f"{CONTINUED_AS_KEY} is one of {', '.join(CONTINUED_AS)}")
        budget = config.get(BUDGET_KEY)
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                                   or not budget > 0):
            raise ValueError(f"{BUDGET_KEY} is a number of US dollars above 0")
        if config.get(SCHEMA_KEY) is not None:
            problem = answer_schema_problem(config[SCHEMA_KEY])
            if problem is not None:
                raise ValueError(f"{SCHEMA_KEY} is not a valid answer schema: {problem}")
        if not config.get("cwd"):
            return config
        return {**config, "cwd": self.check_cwd(config["cwd"])}

    async def create(self, agent: str, input: Input | None = None, *,
                     task_id: str | None = None, config: dict[str, Any] | None = None,
                     queue: str = "default", dedupe_key: str | None = None,
                     history_from: str | None = None,
                     history_through: int | None = None) -> str:
        config = self.check_config(config)
        if self._cwd is not None and not (config or {}).get("cwd"):
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
                     dedupe_key: str | None = None, wake: bool = True) -> int:
        if name not in REQUEST_SIGNALS:
            raise Refused(f"a request may not post the signal {name!r} "
                          f"(only {', '.join(REQUEST_SIGNALS)})")
        if name == MODE_SIGNAL and isinstance(data, Mapping):
            self._mode(data.get("mode"))
        if name == CONFIG_SIGNAL:
            data = self.check_config(data, REQUEST_CHANGEABLE)
            if isinstance(data, Mapping) and data.get(CLIENT_HOOKS_KEY) is not None:
                # Only taken away (a front end a person follows reopening
                # the task): the record keeps other workers from running
                # what a headless client's hook callbacks would judge.
                raise Refused(f"a request may only remove {CLIENT_HOOKS_KEY!r}")
        return await self._client.signal(task_id, name, data, dedupe_key=dedupe_key, wake=wake)

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
    """What a request reaches: the client, that client guarded, and a
    stream-json front on it."""

    client: Any
    guarded: GuardedClient
    front: Any


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
    """The checks before a handler: that a request carries the server's
    bearer token and comes from an allowed ``Host`` and origin; the
    :class:`Scope` every request reaches. ``token_sha256``: the token's
    digest (:func:`token_digest`), ``None`` with ``auth`` off."""

    def __init__(self, client: Any, serving: Any, *, token_sha256: str | None, auth: bool,
                 allowed_hosts: Sequence[str] | None, origins: Sequence[str],
                 allow_bypass: bool, cwd: str | None, roots: Sequence[str]) -> None:
        self._auth = auth
        self._sha256 = token_sha256
        guarded = GuardedClient(client, allow_bypass=allow_bypass, cwd=cwd, roots=roots)
        self.scope = Scope(client, guarded, _GuardedFront(serving, guarded))
        self._hosts = None if allowed_hosts is None else {h.lower() for h in allowed_hosts}
        self._origins = {o.rstrip("/").lower() for o in origins}

    def authorized(self, headers: Any, query: Any = None) -> bool:
        """Whether the request carries the server's token. ``query``: a
        WebSocket's query, whose ``token`` stands in for the header a
        browser cannot set there (no other route takes it, so a token never
        lands in an ordinary URL's logs or history)."""
        if not self._auth:
            return True
        got = headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or (query.get("token", "") if query is not None else "")
        return bool(got) and self._sha256 is not None \
            and hmac.compare_digest(token_digest(got), self._sha256)

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
            if not self.authorized(request.headers):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            request.state.scope = self.scope
            try:
                result: Response = await handler(request)
                return result
            except UnknownTask as exc:
                return JSONResponse({"error": f"unknown task {exc}"}, status_code=404)
            except Refused as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except (msgspec.ValidationError, ValueError, KeyError, IndexError) as exc:
                return JSONResponse({"error": error_text(exc)}, status_code=400)
        return wrapped


def scope(request: Request) -> Scope:
    """The :class:`Scope` the guard set on ``request``."""
    found: Scope = request.state.scope
    return found
