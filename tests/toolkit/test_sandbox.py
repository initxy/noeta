"""The sandbox vocabulary: Claude Code's settings object, domain matching,
and which commands run confined."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from noeta.toolkit.sandbox import (
    SandboxSettings,
    address_refusal,
    domain_allowed,
    leaves_sandbox,
    resolve_allowed,
    runs_sandboxed,
    sandbox_of,
    violations_block,
)


def test_settings_from_claude_codes_object() -> None:
    s = SandboxSettings.from_json({
        "enabled": True, "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
        "excludedCommands": ["docker *"],
        "filesystem": {"allowWrite": ["~/out"], "denyRead": ["~/secrets"], "disabled": False},
        "network": {"allowedDomains": ["github.com", "*.npmjs.org"], "httpProxyPort": 8080,
                    "socksProxyPort": "no", "allowLocalBinding": True},
        "credentials": {"files": [{"path": "~/.netrc", "mode": "mask"},
                                  {"path": "~/x", "mode": "other"}],
                        "envVars": [{"name": "GH_TOKEN", "mode": "deny"}]},
        "enableWeakerNestedSandbox": True, "allowNestedUserNamespaces": True,
        "bwrapPath": "relative/bwrap"})
    assert s.enabled and not s.auto_allow_bash and not s.allow_unsandboxed
    assert s.excluded_commands == ("docker *",)
    assert s.deny_read == ("~/secrets", "~/.netrc") and s.deny_env == ("GH_TOKEN",)
    assert s.allowed_domains == ("github.com", "*.npmjs.org")
    assert s.http_proxy_port == 8080 and s.socks_proxy_port is None
    assert s.allow_local_binding and s.weaker_nested and s.nested_user_namespaces
    assert s.bwrap_path is None  # only an absolute path
    defaults = SandboxSettings.from_json(None)
    assert not defaults.enabled and defaults.auto_allow_bash and defaults.allow_unsandboxed


def test_permission_rules_feed_the_sandbox() -> None:
    s = SandboxSettings.from_settings(
        {"enabled": True, "network": {"allowedDomains": ["a.com"]}},
        {"allow": ["Edit(./build/**)", "WebFetch(domain:b.com)", "Bash(ls)"],
         "deny": ["Read(//etc/shadow)", "Read(bad"], "additionalDirectories": ["../lib"]},
        base_dir="/w")
    assert s.allow_write == ("./build", "../lib")
    assert s.deny_read == ("//etc/shadow",)
    assert s.allowed_domains == ("a.com", "b.com")
    assert s.resolve("//etc/shadow") == "/etc/shadow"
    assert s.resolve("./build") == "/w/build"
    assert s.resolve("x", "/other") == "/other/x"


def test_domain_matching() -> None:
    allowed = ["github.com", "*.npmjs.org", "127.0.0.1:3000", "localhost", "api.x.com:443"]
    assert domain_allowed("github.com", 443, allowed)
    assert domain_allowed("GitHub.com.", 80, allowed)
    assert not domain_allowed("api.github.com", 443, allowed)
    assert domain_allowed("registry.npmjs.org", 443, allowed)
    assert not domain_allowed("npmjs.org", 443, allowed)
    assert domain_allowed("127.0.0.1", 3000, allowed)
    assert not domain_allowed("127.0.0.1", 3001, allowed)
    # A bare local host would open every local port: not honoured.
    assert not domain_allowed("localhost", 8000, allowed)
    assert domain_allowed("api.x.com", 443, allowed)
    assert not domain_allowed("api.x.com", 80, allowed)
    assert not domain_allowed("github.com", 443, allowed, denied=["github.com"])
    assert not domain_allowed("", 80, ["*"])
    assert domain_allowed("anything.io", 1, ["*"])
    # This machine in any spelling needs an entry with its port.
    for host in ("localhost", "127.0.0.2", "[::1]", "[::ffff:127.0.0.1]", "::ffff:7f00:1",
                 "[0:0:0:0:0:0:0:1]", "0:0::1", "::127.0.0.1", "0.0.0.0", "[0::0]"):
        assert not domain_allowed(host, 80, ["*"]), host


def test_an_allowed_name_may_not_resolve_to_a_local_or_metadata_address() -> None:
    allowed = ["*", "10.1.2.3", "fd00:ec2::254:443"]
    assert address_refusal("api.x.com", "93.184.216.34", 443, allowed) is None
    assert address_refusal("corp.example", "10.0.0.7", 443, allowed) is None  # as Claude Code
    for address, reason in (("127.0.0.1", "a loopback address"),
                            ("::ffff:127.0.0.1", "a loopback address"),
                            ("0.0.0.0", "an unspecified address"),
                            ("169.254.169.254", "a link-local address"),
                            ("fe80::1", "a link-local address"),
                            ("100.100.100.200", "a cloud metadata address"),
                            ("224.0.0.1", "a multicast address")):
        assert address_refusal("rebound.example", address, 443, allowed) == reason, address
    # Spellings of an address that only resolve to it are checked the same.
    assert address_refusal("0x7f000001", "127.0.0.1", 80, ["*"]) == "a loopback address"
    assert address_refusal("169.254.169.254", "169.254.169.254", 80, ["*"]) \
        == "a link-local address"
    # Unless an IP entry lets it through, or the name is a loopback one.
    assert address_refusal("x.example", "10.1.2.3", 1, allowed) is None
    assert address_refusal("md.example", "fd00:ec2::254", 443, allowed) is None
    assert address_refusal("md.example", "fd00:ec2::254", 80, allowed) is not None
    assert address_refusal("localhost", "127.0.0.1", 3000, ["localhost:3000"]) is None
    assert address_refusal("localhost", "10.0.0.1", 3000, ["localhost:3000"]) \
        == "a non-loopback address"
    assert address_refusal("x.example", "10.1.2.3", 1, allowed, denied=["10.0.0.0/8"]) \
        == "a deny-listed address"
    assert address_refusal("me.example", "192.168.1.5", 1, ["*"], own=["192.168.1.5"]) \
        == "one of this host's addresses"


async def test_resolution_returns_the_checked_address() -> None:
    assert await resolve_allowed("127.0.0.1", 80, ["127.0.0.1:80"]) == "127.0.0.1"
    with pytest.raises(PermissionError, match="loopback"):
        await resolve_allowed("0x7f000001", 80, ["*"])


@dataclass
class FakeSandbox:
    settings: SandboxSettings
    reason: str | None = None
    prepared: list[Sequence[str]] = field(default_factory=list)

    def unavailable(self) -> str | None:
        return self.reason

    async def prepare(self, argv: Sequence[str], *, cwd: str,
                      writable: Sequence[str] = ()) -> object:
        raise AssertionError("not used")


@dataclass
class Env:
    sandbox: object = None


def test_which_commands_run_sandboxed() -> None:
    on = FakeSandbox(SandboxSettings(enabled=True, excluded_commands=("Bash(docker *)", "make")))
    assert sandbox_of(Env(on)) is on
    assert sandbox_of(Env(FakeSandbox(SandboxSettings()))) is None
    assert sandbox_of(Env("not a sandbox")) is None
    assert runs_sandboxed(on, "ls -la")
    assert not runs_sandboxed(on, "docker ps")
    assert not runs_sandboxed(on, "make")
    assert not runs_sandboxed(on, "ls", True) and not runs_sandboxed(on, "ls", "true")
    assert leaves_sandbox(on, "ls", True) and not leaves_sandbox(on, "docker ps", True)
    assert not leaves_sandbox(on, "ls", False) and not leaves_sandbox(None, "ls", True)
    held = FakeSandbox(SandboxSettings(enabled=True, allow_unsandboxed=False))
    assert runs_sandboxed(held, "ls", True) and not leaves_sandbox(held, "ls", True)
    broken = FakeSandbox(SandboxSettings(enabled=True), reason="no bwrap")
    assert not runs_sandboxed(broken, "ls") and not leaves_sandbox(broken, "ls", True)
    assert not runs_sandboxed(None, "ls")


def test_violations_block() -> None:
    assert violations_block([]) == ""
    block = violations_block(["a.com:443", "a.com:443", "b.com:80"])
    assert block.startswith("<sandbox_violations>\n") and block.endswith("</sandbox_violations>")
    assert block.count("a.com:443") == 1 and "b.com:80" in block


def test_this_hosts_addresses_are_found_without_a_name_lookup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Looking up the host's own name waited on multicast DNS on macOS for
    longer than a connection may take; the routing table answers at once."""
    import socket

    from noeta.toolkit import sandbox

    def no_lookup(*_a: object, **_k: object) -> None:
        raise AssertionError("looked a name up")

    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    own = sandbox._own_addresses()
    assert all(sandbox._address(a) is not None for a in own)
