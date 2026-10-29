"""Secrets in plugin arguments are references (``{env:NAME}``), resolved when
the plugin is made; the reference, not the secret, is what is hashed."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from noeta.agents.agent_files import build_agents, parse_agent_file, resolve_secrets
from noeta.core import registry
from noeta.core.extension import Extension


class Holder(Extension):
    name = "holder"

    def __init__(self, **kw: Any) -> None:
        self.kw = kw


@pytest.fixture(autouse=True)
def holder(monkeypatch: pytest.MonkeyPatch) -> None:
    real = registry.load
    monkeypatch.setattr(registry, "load",
                        lambda group, name: Holder if name == "holder" else real(group, name))


AGENT = parse_agent_file("---\nname: a\nplugins: [holder]\n---\n")
ARGS = {"token": "{env:CHAT_TOKEN}", "header": "Bearer {env:CHAT_TOKEN}",
        "nested": {"keys": ["{env:CHAT_TOKEN}", 3]}}


def test_env_references_resolve_when_the_plugin_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_TOKEN", "s3cret")
    [ext] = build_agents([AGENT], model="m", plugins={"holder": ARGS,
                                                     "tool_search": False})[0].extensions
    assert ext.kw == {"token": "s3cret", "header": "Bearer s3cret",  # type: ignore[attr-defined]
                      "nested": {"keys": ["s3cret", 3]}}
    pinned = registry.pin(ext)["arguments"]
    monkeypatch.setenv("CHAT_TOKEN", "rotated")
    [again] = build_agents([AGENT], model="m", plugins={"holder": ARGS,
                                                       "tool_search": False})[0].extensions
    assert again.kw["token"] == "rotated"  # type: ignore[attr-defined]
    assert registry.pin(again)["arguments"] == pinned  # a rotated secret is the same plugin


def test_an_unset_variable_is_an_error_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    with pytest.raises(ValueError, match=r"plugin 'holder': \{env:NOPE_TOKEN\}"):
        build_agents([AGENT], model="m", plugins={"holder": {"token": "{env:NOPE_TOKEN}"}})


def test_keyring_references(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("keyring")
    fake.get_password = (  # type: ignore[attr-defined]
        lambda service, user: f"{service}:{user}" if user else None)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert resolve_secrets({"t": "{keyring:noeta/chat}"}) == {"t": "noeta:chat"}
    with pytest.raises(ValueError, match="no secret"):
        resolve_secrets("{keyring:noeta}", "x")
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(ValueError, match="needs the keyring package"):
        resolve_secrets("{keyring:noeta/chat}", "x")
