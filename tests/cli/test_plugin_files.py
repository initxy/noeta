"""Plugins as plain files: ``~/.noeta/plugins`` always, a project's
``.noeta/plugins`` once trusted; a broken file is named when its plugin is
asked for; an installed plugin wins a name clash."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.cli import config, home_dir, main
from noeta.cli.config import from_settings, load_settings
from noeta.cli.trust import trust_project
from noeta.core import registry

PLUGIN = '''
from noeta import Extension, text_output


class Wave:
    name = "Wave"
    description = "Wave at someone."
    input_schema = {"type": "object", "properties": {}}
    read_only = True
    clearable = False

    async def run(self, input, ctx):
        return text_output("{word}")


class {cls}(Extension):
    name = "{name}"

    def __init__(self, word: str = "hi") -> None:
        self.word = word

    def tools(self, view):
        return [Wave()]
'''


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = (dict(registry._files), dict(registry._broken), set(registry._loaded_paths))
    yield
    registry._files.clear()
    registry._files.update(saved[0])
    registry._broken.clear()
    registry._broken.update(saved[1])
    registry._loaded_paths.clear()
    registry._loaded_paths.update(saved[2])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)  # as noeta makes it (an open one is a notice)
    (home / "plugins").mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n'
        '[plugins.waver]\nword = "yo"\n[agents.main]\nadd_plugins = ["waver"]\n')
    return home


def _write(path: Path, name: str, cls: str = "Waver") -> None:
    path.write_text(PLUGIN.replace("{name}", name).replace("{cls}", cls)
                    .replace("{word}", "wave"))


async def test_a_file_in_home_plugins_is_a_plugin(home: Path, tmp_path: Path) -> None:
    _write(home / "plugins" / "waver.py", "waver")
    app = from_settings(load_settings(), cwd=str(tmp_path))
    [waver] = [e for e in app.runtime.agents["main"].extensions if e.name == "waver"]
    assert waver.word == "yo"  # type: ignore[attr-defined]
    assert registry.sources(registry.PLUGINS)["waver"] == str(home / "plugins" / "waver.py")
    await app.store.close()


async def test_a_directory_plugin_with_a_relative_import(home: Path, tmp_path: Path) -> None:
    pkg = home / "plugins" / "waver"
    pkg.mkdir()
    (pkg / "words.py").write_text('WORD = "dir"\n')
    _write(pkg / "__init__.py", "waver")
    (pkg / "__init__.py").write_text("from .words import WORD\n"
                                     + (pkg / "__init__.py").read_text())
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert any(e.name == "waver" for e in app.runtime.agents["main"].extensions)
    assert registry.sources(registry.PLUGINS)["waver"] == str(pkg)
    await app.store.close()


def test_a_broken_file_is_named_when_its_plugin_is_asked_for(home: Path,
                                                              tmp_path: Path) -> None:
    (home / "plugins" / "waver.py").write_text("def broken(:\n")
    with pytest.raises(registry.UnknownName, match=r"waver\.py: SyntaxError"):
        from_settings(load_settings(), cwd=str(tmp_path))


def test_an_installed_plugin_wins_a_name_clash(home: Path) -> None:
    _write(home / "plugins" / "mine.py", "web")
    assert registry.load_plugin_files([home / "plugins"]) == []
    assert registry.sources(registry.PLUGINS)["web"] == "package noeta"
    assert "already package noeta" in registry.broken_plugin_files()[
        str(home / "plugins" / "mine.py")]


async def test_project_plugins_load_only_once_trusted(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".noeta" / "plugins").mkdir(parents=True)
    _write(project / ".noeta" / "plugins" / "waver.py", "waver")
    with pytest.raises(registry.UnknownName):
        from_settings(load_settings(), cwd=str(project))
    assert trust_project(str(project)) == str(project.resolve())
    app = from_settings(load_settings(), cwd=str(project))
    assert any(e.name == "waver" for e in app.runtime.agents["main"].extensions)
    assert app.notices == []
    await app.store.close()


async def test_an_untrusted_project_is_a_notice(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".noeta" / "plugins").mkdir(parents=True)
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    app = from_settings(load_settings(), cwd=str(project))
    [notice] = app.notices
    assert "not trusted" in notice and "noeta trust" in notice
    await app.store.close()


def test_the_trust_and_plugins_commands(home: Path, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "repo"
    (project / ".noeta" / "plugins").mkdir(parents=True)
    _write(project / ".noeta" / "plugins" / "waver.py", "waver")
    main.main(["--cwd", str(project), "trust"])
    assert ("prompt overrides, skills and Claude-Code-format settings load"
            in capsys.readouterr().out)
    main.main(["--cwd", str(project), "plugins"])
    out = capsys.readouterr().out
    assert "web\tpackage noeta" in out
    assert f"waver\t{project.resolve() / '.noeta' / 'plugins' / 'waver.py'}" in out


def test_the_example_plugin_works_as_a_plain_directory(home: Path) -> None:
    import shutil

    example = Path(__file__).parents[2] / "examples" / "hello-plugin" / "src" / "noeta_hello"
    shutil.copytree(example, home / "plugins" / "hello",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert registry.load_plugin_files([home / "plugins"]) == ["hello"]
    assert registry.load(registry.PLUGINS, "hello")(greeting="Yo").greeting == "Yo"


def _write_joining(path: Path, name: str, add_to: str) -> None:
    _write(path, name)
    path.write_text(path.read_text().replace(f'    name = "{name}"\n',
                                             f'    name = "{name}"\n    add_to = {add_to}\n'))


async def test_a_file_plugin_with_add_to_joins_its_agents_by_itself(home: Path,
                                                                     tmp_path: Path) -> None:
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    _write_joining(home / "plugins" / "solo.py", "solo", '("main",)')
    _write_joining(home / "plugins" / "everywhere.py", "everywhere", '("*",)')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    names = {a: [e.name for e in agent.extensions] for a, agent in app.runtime.agents.items()}
    assert names["main"][-2:] == ["solo", "everywhere"]
    assert "solo" not in names["Explore"] and "everywhere" in names["Explore"]
    await app.store.close()


async def test_enabled_false_still_wins_over_add_to(home: Path, tmp_path: Path) -> None:
    (home / "config.toml").write_text(
        f'model = "sonnet"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n'
        '[plugins.solo]\nenabled = false\n')
    _write_joining(home / "plugins" / "solo.py", "solo", '("main",)')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    assert all(e.name != "solo" for e in app.runtime.agents["main"].extensions)
    await app.store.close()


async def test_reload_plugins_runs_the_new_code_from_the_next_turn(home: Path,
                                                                    tmp_path: Path) -> None:
    from noeta.cli.config import plugin_changes
    from noeta.core import records
    from noeta.core.events import ToolFinished
    from noeta.testing import ScriptedProvider, reply

    (home / "agents").mkdir()
    (home / "agents" / "plain.md").write_text("---\nname: plain\nplugins: [fs]\n---\nhi\n")
    path = home / "plugins" / "waver.py"
    _write_joining(path, "waver", '("plain",)')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    provider = ScriptedProvider([reply("", ("Wave", {})), reply("one"),
                                 reply("", ("Wave", {})), reply("two")])
    app.runtime.provider_for = lambda _m: provider
    fs_before = next(e for e in app.runtime.agents["plain"].extensions if e.name == "fs")
    tid = await app.client.create("plain", "wave")
    await app.runtime.run_until_idle()

    path.write_text(path.read_text().replace('text_output("wave")', 'text_output("WAVE")'))
    before = dict(app.runtime.agents)
    assert app.reload_plugins is not None and app.reload_plugins() == []
    assert plugin_changes(before, app.runtime.agents) == ["changed waver"]
    fs_after = next(e for e in app.runtime.agents["plain"].extensions if e.name == "fs")
    assert fs_after is fs_before  # an unchanged plugin is the same instance
    await app.client.send(tid, "again")
    await app.runtime.run_until_idle()
    events = await app.client.events(tid)
    assert [r.event.result.text for r in events if isinstance(r.event, ToolFinished)] == [
        "wave", "WAVE"]
    pins = [core.pins for r in events
            if isinstance(core := records.decode(r.event), records.Plugins)]
    assert len(pins) == 2 and pins[0] != pins[1]
    await app.store.close()


async def test_the_machine_policy_allows_and_blocks_plugins(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    managed = tmp_path / "etc-noeta"
    managed.mkdir()
    monkeypatch.setattr(config, "managed_dir", lambda: managed)
    (managed / "config.toml").write_text(
        '[policy]\nallowed_plugins = ["waver"]\nblocked_plugins = ["web"]\n')
    _write_joining(home / "plugins" / "waver.py", "waver", '("main",)')
    (home / "plugins" / "other.py").write_text("raise SystemExit('never imported')\n")
    app = from_settings(load_settings(), cwd=str(tmp_path))
    names = [e.name for e in app.runtime.agents["main"].extensions]
    assert "waver" in names and "web" not in names and "shell" in names
    assert str(home / "plugins" / "other.py") not in registry.broken_plugin_files()
    await app.store.close()
