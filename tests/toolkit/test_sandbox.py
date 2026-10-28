"""The sandbox vocabulary: Claude Code's settings object, domain matching,
and which commands run confined."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from noeta.toolkit.sandbox import (
    SandboxSettings,
    domain_allowed,
    leaves_sandbox,
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
