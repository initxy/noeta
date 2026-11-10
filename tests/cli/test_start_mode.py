"""The permission mode a new task starts in, by the settings files, as the
``noeta`` command tells its front ends and its server's guard: a settings
``disableBypassPermissionsMode`` refuses ``bypassPermissions`` there as the
``--disable-bypass-permissions`` flag does, and follows a reload."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from noeta.cli import home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.host._http_guard import BYPASS_REFUSED, GuardedClient, Refused
from noeta.host.app import bypass_allowed, start_mode


async def test_the_settings_say_what_mode_a_task_starts_in(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(home_dir, "HOME", home)
    home.mkdir()
    (home / "config.toml").write_text(
        f'model = "m"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "t.db"}"\n')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    try:
        # What noeta serve's guard asks at each request.
        guard = GuardedClient(app.client, cwd=None, allow_bypass=lambda: bypass_allowed(app))
        bypass = {"permission_mode": "bypassPermissions"}
        assert start_mode(app, None) == "bypassPermissions" and guard.check_config(bypass)
        settings = home / "settings.json"
        settings.write_text(json.dumps({"permissions": {
            "disableBypassPermissionsMode": "disable", "defaultMode": "acceptEdits"}}))
        assert app.reload_plugins is not None
        app.reload_plugins()
        assert [start_mode(app, m) for m in (None, "bypassPermissions", "default")] == [
            "acceptEdits", "acceptEdits", "default"]
        with pytest.raises(Refused) as refused:
            guard.check_config(bypass)
        assert refused.value.code == BYPASS_REFUSED
        settings.write_text(json.dumps({"permissions": {
            "disableBypassPermissionsMode": "disable"}}))
        app.reload_plugins()
        assert start_mode(app, None) == "auto"
    finally:
        await app.store.close()
