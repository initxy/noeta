"""``noeta service``: the systemd user unit and the launchd LaunchAgent it
writes (pure text, compared whole), and the commands it runs through its one
runner (a fake here)."""

from __future__ import annotations

import plistlib
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from noeta.cli import home_dir, main, service_command
from noeta.cli.service_command import Service

UNIT = """\
# Written by noeta service install; run it again to change this unit.
[Unit]
Description=noeta serve (the HTTP API and the web app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/noeta/bin/noeta --model "my model" serve --host 0.0.0.0 --allow-host 100%%.example "--allow-origin" "a$$b\\"c"
WorkingDirectory=/home/me
EnvironmentFile=/home/me/.config/systemd/user/noeta.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def test_the_unit_runs_the_command_with_its_words_taken_literally() -> None:
    argv = ["/opt/noeta/bin/noeta", "--model", "my model", "serve", "--host", "0.0.0.0",
            "--allow-host", "100%.example", "--allow-origin", 'a$b"c']
    got = service_command.systemd_unit(argv, home="/home/me",
                                       env_file="/home/me/.config/systemd/user/noeta.env")
    # --allow-origin is plain; quoting it anyway would be harmless, the snapshot is exact.
    assert got == UNIT.replace('"--allow-origin"', "--allow-origin")
    assert service_command._unit_argv(got) == argv


def test_the_env_file_and_what_it_takes_from_the_shell() -> None:
    environ = {"NOETA_MODEL": "sonnet", "ANTHROPIC_API_KEY": "sk-1", "OPENAI_BASE_URL": "u",
               "DEEPSEEK_API_KEY": "d", "https_proxy": "http://p:1", "NO_PROXY": "localhost",
               "PATH": "/usr/bin", "HOME": "/home/me", "TERM": "xterm", "AWS_KEY": "no",
               "NOETA_ODD": 'a"b\\c', "NOETA_LINES": "a\nb"}
    env = service_command.service_env(environ)
    assert set(env) == {"NOETA_MODEL", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL",
                        "DEEPSEEK_API_KEY", "https_proxy", "NO_PROXY", "PATH", "HOME",
                        "NOETA_ODD", "NOETA_LINES"}
    assert service_command.systemd_env_file(env) == (
        "# Written by noeta service install: the installing shell's variables.\n"
        'ANTHROPIC_API_KEY="sk-1"\nDEEPSEEK_API_KEY="d"\nHOME="/home/me"\n'
        'NOETA_MODEL="sonnet"\nNOETA_ODD="a\\"b\\\\c"\nNO_PROXY="localhost"\n'
        'OPENAI_BASE_URL="u"\nPATH="/usr/bin"\nhttps_proxy="http://p:1"\n')


def test_the_launch_agent_keeps_the_server_running_and_logs_to_noetas_home() -> None:
    data = service_command.launchd_plist(["/n", "serve"], home="/Users/me",
                                         env={"PATH": "/bin"}, log="/Users/me/.noeta/logs/serve.log")
    assert plistlib.loads(data) == {
        "Label": "dev.noeta.serve", "ProgramArguments": ["/n", "serve"],
        "WorkingDirectory": "/Users/me", "EnvironmentVariables": {"PATH": "/bin"},
        "RunAtLoad": True, "KeepAlive": True,
        "StandardOutPath": "/Users/me/.noeta/logs/serve.log",
        "StandardErrorPath": "/Users/me/.noeta/logs/serve.log"}


class Fake(service_command.Runner):
    """Records each command; answers from ``answers`` (by its first words)."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.ran: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv: Sequence[str], *,
                 capture: bool = True) -> subprocess.CompletedProcess[str]:
        self.ran.append(list(argv))
        for n in range(len(argv), 0, -1):
            if tuple(argv[:n]) in self.answers:
                code, out = self.answers[tuple(argv[:n])]
                return subprocess.CompletedProcess(list(argv), code, out, "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "noeta-home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / "noeta")])
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "noeta").write_text("#!/bin/sh\n")
    return tmp_path


def _systemd(home: Path, fake: Fake) -> Service:
    return Service("systemd", fake, home / ".config" / "systemd" / "user", str(home), 1000, "me")


def _service(argv: list[str], service: Service, environ: dict[str, str]) -> int:
    args = main._parser().parse_args(argv)
    return service_command.run(args, serve_arguments=main._serve_arguments,
                               common=main._common, service=service, environ=environ)


def test_install_writes_the_unit_starts_it_and_shows_where_to_open_it(
        home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({("systemctl", "--user", "is-active"): (0, "active\n"),
                 ("loginctl",): (0, "no\n")})
    service = _systemd(home, fake)
    environ = {"NOETA_MODEL": "m", "PATH": "/usr/bin", "SECRET_ELSEWHERE": "x"}
    assert _service(["--model", "m", "service", "install", "--port", "18642",
                     "--web", "dist"], service, environ) == 0
    noeta = str(home / "bin" / "noeta")
    import os
    assert service.argv() == [noeta, "--model", "m", "serve", "--port", "18642",
                              "--web", os.path.abspath("dist")]
    assert stat.S_IMODE(service.env_path.stat().st_mode) == 0o600
    assert service.env() == {"NOETA_MODEL": "m", "PATH": "/usr/bin"}
    assert fake.ran[:3] == [["systemctl", "--user", "daemon-reload"],
                            ["systemctl", "--user", "enable", "noeta.service"],
                            ["systemctl", "--user", "restart", "noeta.service"]]
    out = capsys.readouterr().out
    token = (home / "noeta-home" / "server-token").read_text().strip()
    assert f"noeta: at http://127.0.0.1:18642/#token={token}" in out
    assert "installed" in out and "active" in out
    assert "loginctl enable-linger me" in out
    # Again: replaced, the same token.
    assert _service(["service", "install", "--port", "18642"], service, environ) == 0
    out = capsys.readouterr().out
    assert "replaced" in out and f"#token={token}" in out


def test_status_logs_control_and_uninstall(
        home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({("systemctl", "--user", "is-active"): (0, "active\n"),
                 ("loginctl",): (0, "yes\n")})
    service = _systemd(home, fake)
    assert _service(["service", "status"], service, {}) == 1
    assert "not installed" in capsys.readouterr().err
    _service(["service", "install", "--no-token"], service, {"NOETA_SERVER_TOKEN": "tok"})
    assert "linger" not in capsys.readouterr().out
    assert _service(["service", "status"], service, {}) == 0
    out = capsys.readouterr().out
    assert out.startswith("noeta service: active (") and "at http://127.0.0.1:8642/\n" in out
    fake.ran.clear()
    for action in ("stop", "start", "restart"):
        assert _service(["service", action], service, {}) == 0
    assert _service(["service", "logs", "--no-follow"], service, {}) == 0
    assert _service(["service", "logs"], service, {}) == 0
    assert [r for r in fake.ran if "is-active" not in r] == [
        ["systemctl", "--user", "stop", "noeta.service"],
        ["systemctl", "--user", "start", "noeta.service"],
        ["systemctl", "--user", "restart", "noeta.service"],
        ["journalctl", "--user", "-u", "noeta.service", "-n", "200", "--no-pager"],
        ["journalctl", "--user", "-u", "noeta.service", "-n", "200", "--no-pager", "-f"]]
    fake.ran.clear()
    assert _service(["service", "uninstall"], service, {}) == 0
    assert fake.ran == [["systemctl", "--user", "disable", "--now", "noeta.service"],
                        ["systemctl", "--user", "daemon-reload"]]
    assert not service.path.exists() and not service.env_path.exists()


def test_the_configured_token_is_the_one_status_shows(
        home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service = _systemd(home, Fake({("loginctl",): (0, "yes\n")}))
    _service(["service", "install", "--host", "127.0.0.1"], service,
             {"NOETA_SERVER_TOKEN": "a b"})
    assert not (home / "noeta-home" / "server-token").exists()
    _service(["service", "status"], service, {})
    assert "#token=a b" in capsys.readouterr().out


def test_a_failing_step_is_reported(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({("systemctl", "--user", "restart"): (1, "")})
    service = _systemd(home, fake)
    assert _service(["service", "install"], service, {}) == 1
    assert "systemctl --user restart noeta.service: exit 1" in capsys.readouterr().err


def test_launchd_install_and_the_rest(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({("launchctl", "print"): (0, "gui/501/dev.noeta.serve = {\n\tstate = running\n")})
    agents = home / "Library" / "LaunchAgents"
    service = Service("launchd", fake, agents, str(home), 501, "me")
    assert _service(["service", "install"], service, {"PATH": "/bin"}) == 0
    assert stat.S_IMODE(service.path.stat().st_mode) == 0o600
    assert service.env() == {"PATH": "/bin"}
    assert fake.ran[:2] == [["launchctl", "bootout", "gui/501/dev.noeta.serve"],
                            ["launchctl", "bootstrap", "gui/501", str(service.path)]]
    assert "running" in capsys.readouterr().out
    fake.ran.clear()
    for action in ("stop", "start", "restart"):
        _service(["service", action], service, {})
    _service(["service", "logs", "--no-follow"], service, {})
    _service(["service", "uninstall"], service, {})
    assert [r for r in fake.ran if r[:2] != ["launchctl", "print"]] == [
        ["launchctl", "bootout", "gui/501/dev.noeta.serve"],
        ["launchctl", "kickstart", "-k", "gui/501/dev.noeta.serve"],
        ["tail", "-n", "200", str(home / "noeta-home" / "logs" / "serve.log")],
        ["launchctl", "bootout", "gui/501/dev.noeta.serve"]]
    assert not service.path.exists()


def test_without_a_service_manager_it_says_to_use_tmux(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({("systemctl", "--user", "show-environment"): (1, "")})
    assert service_command.detect(fake, platform="linux", home="/h") is None
    assert service_command.detect(fake, platform="win32", home="/h") is None
    monkeypatch.setattr(service_command, "detect", lambda: None)
    args = main._parser().parse_args(["service", "status"])
    assert service_command.run(args, serve_arguments=main._serve_arguments,
                               common=main._common) == 1
    assert "tmux" in capsys.readouterr().err


def test_detect_finds_the_user_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_command.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    found = service_command.detect(Fake(), platform="linux", home="/h")
    assert found is not None and found.path == Path("/h/.config/systemd/user/noeta.service")
    mac = service_command.detect(Fake(), platform="darwin", home="/h")
    assert mac is not None and mac.path == Path("/h/Library/LaunchAgents/dev.noeta.serve.plist")


def test_install_refuses_what_serve_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_settings", lambda _a: object())
    with pytest.raises(SystemExit, match="loopback"):
        main.main(["service", "install", "--host", "0.0.0.0", "--no-token"])


def test_restart_if_installed(home: Path) -> None:
    service = _systemd(home, Fake())
    assert service_command.restart_if_installed(service) is None
    service.path.parent.mkdir(parents=True)
    service.path.write_text("ExecStart=/n serve\n")
    assert service_command.restart_if_installed(service) is True
    failing = _systemd(home, Fake({("systemctl", "--user", "restart"): (1, "")}))
    assert service_command.restart_if_installed(failing) is False
