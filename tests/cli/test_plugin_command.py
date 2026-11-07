"""``noeta plugin add|list|remove``: pinned installs into ~/.noeta/plugins,
and a Claude Code marketplace read only to find where a plugin comes from."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.cli import home_dir, main, plugin_command
from noeta.cli.config import from_settings, load_settings
from noeta.cli.plugin_command import PluginInstallError, install
from noeta.core import registry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PLUGIN = '''from noeta import Extension


class Pinned(Extension):
    name = "pinned"
    add_to = ("main",)
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def repo(path: Path, files: dict[str, str]) -> str:
    """A git repository holding ``files``; returns its commit."""
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "one")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = dict(registry._files), dict(registry._broken), set(registry._loaded_paths)
    yield
    for table, value in zip((registry._files, registry._broken, registry._loaded_paths), saved,
                            strict=True):
        table.clear()
        table.update(value)  # type: ignore[attr-defined]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    return home


async def test_a_git_plugin_installs_at_its_commit_and_loads(home: Path, tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]
                                                             ) -> None:
    src = tmp_path / "pinned-plugin"
    src.mkdir()
    first = repo(src, {"__init__.py": PLUGIN})
    (src / "__init__.py").write_text("raise SystemExit('a later commit')\n")
    git(src, "commit", "-qam", "two")
    url = f"file://{src}"
    with pytest.raises(PluginInstallError, match="needs @<commit>"):
        install(url)
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "add", f"{url}@{first}"])
    assert "installed pinned-plugin" in capsys.readouterr().out
    installed = home / "plugins" / "pinned-plugin"
    assert (installed / "__init__.py").read_text() == PLUGIN and not (installed / ".git").exists()
    record = json.loads((home / "plugin-sources.json").read_text())["pinned-plugin"]
    assert record == {"source": url, "commit": first, "path": str(installed)}
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert any(e.name == "pinned" for e in app.runtime.agents["main"].extensions)
    await app.store.close()
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "list"])
    assert f"pinned-plugin\t{url}@{first}" in capsys.readouterr().out
    with pytest.raises(PluginInstallError, match="already"):
        install(f"{url}@{first}")
    with pytest.raises(SystemExit) as done:
        main.main(["plugin", "remove", "pinned-plugin"])
    assert done.value.code == 0 and not installed.exists()
    assert json.loads((home / "plugin-sources.json").read_text()) == {}


def test_a_pin_is_a_full_commit_over_an_encrypted_transport(home: Path,
                                                            tmp_path: Path) -> None:
    src = tmp_path / "short-pinned"
    src.mkdir()
    first = repo(src, {"__init__.py": PLUGIN})
    # A short id could match a commit made later to collide with it.
    for short in (first[:7], first[:12], first[:39]):
        with pytest.raises(PluginInstallError, match="full commit id"):
            install(f"file://{src}@{short}")
    with pytest.raises(PluginInstallError, match=r"40 hex digits, or 64 in a SHA-256"):
        install(f"file://{src}", commit=first[:10])
    # Plain-text transports: anyone on the way could hand over other code.
    for url in ("http://example.invalid/p.git", "git://example.invalid/p.git"):
        with pytest.raises(PluginInstallError, match="not a git URL"):
            install(f"{url}@{first}")
    assert not (home / "plugins").exists()


def test_a_local_file_is_copied(home: Path, tmp_path: Path) -> None:
    (tmp_path / "solo.py").write_text(PLUGIN)
    name, path = install(str(tmp_path / "solo.py"))
    assert (name, path) == ("solo", home / "plugins" / "solo.py")


def test_a_marketplace_is_read_to_find_its_plugins(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "formatter"
    other.mkdir()
    sha = repo(other, {".claude-plugin/plugin.json": '{"name": "formatter"}',
                       "commands/fmt.md": "Format $ARGUMENTS.\n"})
    market = tmp_path / "market"
    market.mkdir()
    listing = {"name": "team", "owner": {"name": "t"}, "metadata": {"pluginRoot": "./plugins"},
               "plugins": [
                   {"name": "local-one", "source": "local-one"},
                   {"name": "formatter", "source": {"source": "url", "url": f"file://{other}",
                                                     "sha": sha}},
                   {"name": "floating", "source": {"source": "url", "url": f"file://{other}"}},
                   {"name": "branch", "source": {"source": "url", "url": f"file://{other}",
                                                  "ref": "main"}},
                   {"name": "npm-one", "source": {"source": "npm", "package": "x"}},
                   {"name": "escape", "source": "./../formatter"}]}
    market_sha = repo(market, {".claude-plugin/marketplace.json": json.dumps(listing),
                               "plugins/local-one/skills/a/SKILL.md": "---\nname: a\n---\nA\n"})
    name, path = install("local-one", marketplace=f"file://{market}@{market_sha}")
    assert name == "local-one" and (path / "skills" / "a" / "SKILL.md").is_file()
    name, path = install("formatter", marketplace=str(market))
    assert name == "formatter" and (path / "commands" / "fmt.md").is_file()
    with pytest.raises(PluginInstallError, match="pins no commit"):
        install("floating", marketplace=str(market))
    with pytest.raises(PluginInstallError, match="its ref 'main' is not followed"):
        install("branch", marketplace=str(market))
    with pytest.raises(PluginInstallError, match="'npm' source is not supported"):
        install("npm-one", marketplace=str(market))
    with pytest.raises(PluginInstallError, match="outside"):
        install("escape", marketplace=str(market))
    with pytest.raises(PluginInstallError, match="lists no plugin 'nope'"):
        install("nope", marketplace=str(market))
    assert json.loads(listing_text := (market / ".claude-plugin" / "marketplace.json")
                      .read_text()) == listing, listing_text  # only read


def test_a_marketplace_url_that_is_not_a_git_url_is_refused(home: Path,
                                                            tmp_path: Path) -> None:
    pwned = tmp_path / "pwned"
    market = tmp_path / "market"
    (market / ".claude-plugin").mkdir(parents=True)
    sha = "a" * 40
    listing = {"name": "m", "plugins": [
        {"name": "flag", "source": {"source": "url", "url": f"--upload-pack=touch {pwned}",
                                    "sha": sha}},
        {"name": "sub", "source": {"source": "git-subdir", "url": f"-uploadpack=touch {pwned}",
                                   "sha": sha}},
        {"name": "plain", "source": {"source": "url", "url": "/some/where", "sha": sha}}]}
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(listing))
    for name in ("flag", "sub", "plain"):
        with pytest.raises(PluginInstallError, match="not a git URL"):
            install(name, marketplace=str(market))
    assert not pwned.exists()


def _market(path: Path, entries: list[dict[str, object]], files: dict[str, str] | None = None,
            name: str = "team") -> str:
    """A marketplace repository listing ``entries``; returns its commit."""
    path.mkdir(exist_ok=True)
    listing = {"name": name, "owner": {"name": "t"}, "plugins": entries}
    return repo(path, {".claude-plugin/marketplace.json": json.dumps(listing), **(files or {})})


def _configure(home: Path, table: dict[str, str]) -> None:
    (home / "marketplaces.json").write_text(json.dumps(
        {"marketplaces": {n: {"source": s} for n, s in table.items()}}))


def test_the_official_marketplace_is_configured_until_removed(
        home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert plugin_command.marketplaces() == {"noeta-plugins": plugin_command.OFFICIAL_SOURCE}
    market = tmp_path / "market"
    _market(market, [{"name": "local-one", "source": "./local-one", "version": "1.2.0"}],
            {"local-one/__init__.py": PLUGIN})
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "marketplace", "remove", "noeta-plugins"])
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "marketplace", "add", str(market)])
    assert f"added marketplace team from {market} (1 plugins)" in capsys.readouterr().out
    assert plugin_command.marketplaces() == {"team": str(market)}
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "marketplace", "list"])
    assert capsys.readouterr().out == (f"team\t{market}\n"
                                       "  local-one\t1.2.0\t(pins no commit)\n")
    with pytest.raises(SystemExit, match="1"):
        main.main(["plugin", "marketplace", "remove", "nope"])
    with pytest.raises(SystemExit, match="1"):
        main.main(["plugin", "marketplace", "add", str(tmp_path)])
    assert "no .claude-plugin/marketplace.json" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="0"):
        main.main(["plugin", "marketplace", "remove", "team"])
    assert plugin_command.marketplaces() == {}


def test_market_entries_read_every_marketplace(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "formatter"
    other.mkdir()
    sha = repo(other, {"__init__.py": PLUGIN})
    market = tmp_path / "market"
    commit = _market(market, [
        {"name": "local-one", "source": "local-one", "description": "d",
         "noeta": {"ui_api": 1, "parts": ["backend", "web", 3]}},
        {"name": "formatter", "source": {"source": "git-subdir", "url": f"file://{other}",
                                         "path": "sub", "sha": sha.upper()}},
        {"name": "npm-one", "source": {"source": "npm", "package": "x"}},
        {"source": "./nameless"}])
    _configure(home, {"local": str(market), "pinned": f"file://{market}@{commit}",
                      "branch": f"file://{market}", "broken": str(tmp_path / "nowhere")})
    (home / "plugins").mkdir()
    (home / "plugins" / "formatter").mkdir()
    notices: list[str] = []
    entries = plugin_command.market_entries(notices)
    [notice] = notices
    assert notice.startswith(f"marketplace broken ({tmp_path / 'nowhere'}) could not be read")
    by = {(e["marketplace"], e["name"]): e for e in entries}
    assert sorted(by) == sorted((m, n) for m in ("local", "pinned", "branch")
                                for n in ("local-one", "formatter", "npm-one"))
    assert by["local", "local-one"] == {
        "marketplace": "local", "name": "local-one", "description": "d", "version": "",
        "source": str(market / "local-one"), "commit": None, "parts": ["backend", "web"],
        "ui_api": 1, "installed": False}
    assert by["pinned", "local-one"]["source"] == f"file://{market}#local-one"
    assert by["pinned", "local-one"]["commit"] == commit
    assert by["branch", "local-one"]["commit"] is None  # a branch can move: no pin
    assert by["branch", "formatter"] == {
        "marketplace": "branch", "name": "formatter", "description": "", "version": "",
        "source": f"file://{other}#sub", "commit": sha, "parts": [], "ui_api": None,
        "installed": True}
    assert by["local", "npm-one"]["source"] == "a 'npm' source (not supported)"


def test_a_marketplace_at_its_branch_is_kept_for_a_while(home: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    market = tmp_path / "market"
    _market(market, [{"name": "one", "source": "./one"}])
    _configure(home, {"team": f"file://{market}"})
    assert [e["name"] for e in plugin_command.market_entries()] == ["one"]
    listing = {"name": "team", "plugins": [{"name": "two", "source": "./two"}]}
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(listing))
    git(market, "commit", "-qam", "two")
    assert [e["name"] for e in plugin_command.market_entries()] == ["one"]
    assert [e["name"] for e in plugin_command.market_entries(refresh=True)] == ["two"]
    monkeypatch.setattr(plugin_command, "MARKET_TTL", 0.0)
    shutil.rmtree(market)
    notices: list[str] = []
    assert plugin_command.market_entries(notices) == [] and "git clone failed" in notices[0]


def test_a_bare_name_installs_from_the_marketplace_listing_it(home: Path,
                                                              tmp_path: Path) -> None:
    plugins = tmp_path / "plugins-repo"
    plugins.mkdir()
    sha = repo(plugins, {"guard/__init__.py": PLUGIN, "other/__init__.py": PLUGIN})
    entry = {"name": "guard", "source": {"source": "git-subdir", "url": f"file://{plugins}",
                                         "path": "guard", "sha": sha}}
    first, second = tmp_path / "first", tmp_path / "second"
    _market(first, [entry, {"name": "loose", "source": "./other"}])
    _market(second, [entry, {"name": "solo", "source": {"source": "url",
                                                        "url": f"file://{plugins}",
                                                        "sha": sha}}], name="more")
    _configure(home, {"team": f"file://{first}", "more": str(second)})
    with pytest.raises(PluginInstallError, match=r"several marketplaces list 'guard' \(more, "
                                                 r"team\)"):
        install("guard")
    with pytest.raises(PluginInstallError, match="no configured marketplace lists a plugin "
                                                 "'nope'"):
        install("nope")
    with pytest.raises(PluginInstallError, match="read at its default branch"):
        install("loose")
    with pytest.raises(PluginInstallError, match=f"pins {sha}, not {'b' * 40}"):
        install("guard", marketplace="team", commit="b" * 40)
    name, path = install("guard", marketplace="team", commit=sha)
    assert name == "guard" and (path / "__init__.py").read_text() == PLUGIN
    assert json.loads((home / "plugin-sources.json").read_text())["guard"] == {
        "marketplace": "team", "plugin": "guard", "commit": sha, "path": str(path)}
    name, path = install("solo")
    assert name == "solo" and (path / "guard" / "__init__.py").is_file()
    installed = {e["name"]: e["installed"] for e in plugin_command.market_entries()}
    assert installed == {"guard": True, "loose": False, "solo": True}


async def test_a_config_naming_a_plugin_that_left_noeta_says_how_to_get_it(
        home: Path, tmp_path: Path) -> None:
    with (home / "config.toml").open("a") as f:
        f.write('[plugins.repetition_guard]\nthreshold = 4\n'
                '[plugins.gone_plugin]\nx = 1\n[plugins.off_one]\nenabled = false\n'
                '[agents.main]\nadd_plugins = ["repetition_guard"]\n')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    line = ("repetition_guard is no longer part of noeta; it is in the noeta-plugins "
            "marketplace: noeta plugin add repetition_guard")
    assert [n for n in app.notices if n.startswith("config.toml")] == [
        f"config.toml [plugins.repetition_guard]: {line}",
        "config.toml [plugins.gone_plugin]: no plugin gone_plugin is installed here "
        "(noeta plugins lists them)",
        f"config.toml [agents.main] add_plugins: {line}"]
    assert not any(e.name == "repetition_guard" for e in app.runtime.agents["main"].extensions)
    await app.store.close()
