"""WebFetch egress policy — which hosts a fetch may reach without asking.

``WebFetch`` takes its ``url`` straight from the model, and the tool is
``risk_level="low"``, so the static approval set never gates it. The fence is
therefore a per-call one, decided from the URL:

**The host allowlist** (:func:`normalize_allowed_hosts`,
:func:`host_in_allowlist`). ``HostConfig.webfetch_allowed_hosts`` is operator
configuration — trusted like the shell allowlist — naming the hosts a fetch
may reach without asking a human. Under a gating permission mode every other
host asks, per call. It is judged on the URL's **real** host:
``https://allowed.com@evil.com/`` is judged as ``evil.com``, because
``allowed.com`` there is userinfo, not a host. Matching is on the whole
normalised host, never on a substring of the URL.

**The scheme check** (:func:`unsupported_scheme_refusal`). ``WebFetch`` fetches
``http(s)`` and nothing else. It refuses no host: a ``file:`` URL is refused
because fetching one is not what the tool does, not because of where it points.

This module lives beside the host rather than inside the ``web`` built-in
because :mod:`noeta.client.host` builds the approval predicate and cannot
statically import ``noeta.builtins``; the built-in imports down into it, so
there is one implementation of "what host is this URL really for".
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence
from urllib.parse import unquote, urlsplit


__all__ = [
    "host_in_allowlist",
    "normalize_allowed_hosts",
    "unsupported_scheme_refusal",
    "url_host",
]


#: One label of a hostname allowlist entry, after normalisation.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")

#: The schemes ``WebFetch`` fetches. Refused at the tool boundary rather than
#: left to the transport: the local httpx transport rejects the rest itself,
#: but the sandbox transport hands the URL to ``curl``, which would happily
#: read a container file for a ``file:`` URL and call it a page.
_FETCH_SCHEMES = frozenset({"http", "https"})


# ---------------------------------------------------------------------------
# host normalisation
# ---------------------------------------------------------------------------


def _idna(host: str) -> str:
    """``host`` as ASCII (IDNA/punycode), lowercased.

    A host that IDNA cannot encode (an over-long label, an empty one) is
    returned lowercased and otherwise untouched: it is not a valid name, so it
    fails the allowlist and the call falls through to the approval gate.
    """
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return host.lower()


def url_host(url: str) -> Optional[str]:
    """The URL's **real** host, normalised; ``None`` when it names none.

    ``urlsplit().hostname`` already drops userinfo and the IPv6 brackets and
    lowercases, so ``https://allowed.com@evil.com/`` answers ``evil.com``. On
    top of that: percent-escapes are decoded, a trailing root dot is dropped
    (``example.com.`` and ``example.com`` are one host) and a name is
    IDNA-normalised, so the Unicode and punycode spellings of one host compare
    equal — the operator lists a host once and it matches however the model
    spells it.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = unquote(host).strip().rstrip(".").lower()
    if not host:
        return None
    return _idna(host)


# ---------------------------------------------------------------------------
# the operator's host allowlist
# ---------------------------------------------------------------------------


def normalize_allowed_hosts(entries: Iterable[object]) -> tuple[str, ...]:
    """Validate and normalise ``HostConfig.webfetch_allowed_hosts``.

    Two forms, and only two:

    * ``example.com`` — that host exactly.
    * ``*.example.com`` — any subdomain of it, at any depth, but **not**
      ``example.com`` itself. List both to cover the apex too.

    Everything else raises :class:`ValueError` at construction rather than
    silently matching nothing (a security knob that fails open on a typo is
    worse than no knob): a scheme, a path, a port, userinfo, a query, a ``*``
    anywhere but as a leading ``*.``, an empty entry, or a label that is not a
    hostname label.
    """
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            raise ValueError(
                "webfetch_allowed_hosts entries must be strings like "
                f"'example.com' or '*.example.com'; got {raw!r}"
            )
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            raise ValueError(
                "webfetch_allowed_hosts entry is empty; write a host like "
                "'example.com' or '*.example.com'"
            )
        for bad, why in (
            ("://", "a scheme"),
            ("/", "a path"),
            ("@", "userinfo"),
            ("?", "a query"),
            (" ", "whitespace"),
            (":", "a colon — the list names hostnames, never ports"),
        ):
            if bad in entry:
                raise ValueError(
                    f"webfetch_allowed_hosts entry {raw!r} carries {why}; "
                    "write the host alone, e.g. 'example.com' or "
                    "'*.example.com'"
                )
        wildcard = entry.startswith("*.")
        name = _idna(entry[2:] if wildcard else entry)
        if not name or "*" in name:
            raise ValueError(
                f"webfetch_allowed_hosts entry {raw!r} is not a host; the only "
                "wildcard form is a leading '*.' (e.g. '*.example.com')"
            )
        if any(not _LABEL_RE.match(label) for label in name.split(".")):
            raise ValueError(
                f"webfetch_allowed_hosts entry {raw!r} is not a valid hostname"
            )
        out.append(f"*.{name}" if wildcard else name)
    return tuple(out)


def host_in_allowlist(host: Optional[str], allowed: Sequence[str]) -> bool:
    """Is ``host`` (already normalised) covered by ``allowed``?

    Whole-host matching only: ``allowed.com`` does not cover
    ``notallowed.com``, and ``*.allowed.com`` covers ``www.allowed.com`` but
    neither ``allowed.com`` nor ``evil-allowed.com``. ``None`` (a URL with no
    host) matches nothing, so a URL the gate cannot read asks a human.
    """
    if not host:
        return False
    for entry in allowed:
        if entry.startswith("*."):
            suffix = entry[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == entry:
            return True
    return False


# ---------------------------------------------------------------------------
# the scheme check
# ---------------------------------------------------------------------------


def unsupported_scheme_refusal(url: str) -> Optional[str]:
    """The refusal text for a URL ``WebFetch`` does not fetch, else ``None``.

    Only the scheme is judged, and only when the URL names one: a scheme-less
    URL falls through to the transport, which reports "no scheme" far more
    usefully than this could.
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return None
    if scheme and scheme not in _FETCH_SCHEMES:
        return f"WebFetch fetches http(s) URLs only; {scheme}: is not fetched"
    return None
