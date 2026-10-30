"""A Claude Code plugin's ``userConfig``: the values it asks the person for.

The manifest's ``userConfig`` maps each option key to its field (``type``
``string`` / ``number`` / ``boolean`` / ``directory`` / ``file``, ``title``,
``description``, ``required``, ``default``, ``multiple``, ``sensitive``,
``min`` / ``max``, ``options``). ``noeta plugin add`` (and ``noeta plugin
configure``) asks for the values, or takes ``--config KEY=VALUE``, as Claude
Code does. They are kept where Claude Code keeps them, in noeta's own
place: ``~/.noeta/settings.json`` ``pluginConfigs.<plugin>.options``. A
sensitive value goes to the OS keyring (service :data:`KEYRING_SERVICE`,
account ``<plugin>/<key>``) and the options hold its ``{keyring:…}``
reference, which is resolved only when the plugin that uses it is made
(``agent_files.resolve_secrets``); a value typed as ``{env:NAME}`` is kept
as that reference.

``${user_config.KEY}`` is substituted as Claude Code does: in MCP server
configs and hook commands every value (a shell-form hook command may not use
one, its ``args`` may), in commands and agent prompts the non-sensitive ones
only. Hooks get each value as ``CLAUDE_PLUGIN_OPTION_<KEY>``. What cannot be used
on this machine (a key unset, a secret it cannot read) is decided per option,
server and hook when the plugin is read (:mod:`noeta.cli.claude_plugins`),
never by stopping noeta.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from noeta.cli import home_dir

__all__ = ["KEYRING_SERVICE", "OptionError", "configure", "content", "forget", "hook_env",
           "load", "schema", "substitute", "uses"]

KEYRING_SERVICE = "noeta-plugin"
SETTINGS_FILE = "settings.json"
TYPES = ("string", "number", "boolean", "directory", "file")
_KEY = re.compile(r"^[A-Za-z_]\w*$")
_REF = re.compile(r"\$\{user_config\.([^}]+)\}")
_SECRET_REF = re.compile(r"^\{(env|keyring):[^{}]+\}$")
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


class OptionError(ValueError):
    pass


def schema(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The manifest's ``userConfig``: the fields with a valid key and type."""
    raw = manifest.get("userConfig")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): dict(v) for k, v in raw.items()
            if isinstance(k, str) and _KEY.match(k) and isinstance(v, Mapping)
            and v.get("type") in TYPES}


def _settings_path() -> Path:
    return home_dir.HOME / SETTINGS_FILE


def _read_settings() -> dict[str, Any]:
    path = _settings_path()
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OptionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OptionError(f"{path} is not a JSON object")
    return data


def _write_settings(data: dict[str, Any]) -> None:
    """Replace the settings file at once (it holds permission rules too)."""
    home_dir.ensure_home()
    path = _settings_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)


def stored(plugin: str) -> dict[str, Any]:
    """The values kept for ``plugin`` (references unresolved)."""
    try:
        data = _read_settings()
    except OptionError:
        return {}
    entry = (data.get("pluginConfigs") or {}).get(plugin)
    options = entry.get("options") if isinstance(entry, dict) else None
    return dict(options) if isinstance(options, dict) else {}


def load(plugin: str, fields: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """``plugin``'s values: the kept ones, else each field's default."""
    kept = stored(plugin)
    out = {k: f["default"] for k, f in fields.items() if f.get("default") is not None}
    out.update({k: v for k, v in kept.items() if k in fields and v not in (None, "")})
    return out


def _text(value: Any) -> str:
    """A value as Claude Code's ``String(value)`` writes it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return ",".join(_text(v) for v in value)
    return str(value)


def substitute(value: Any, values: Mapping[str, Any]) -> Any:
    """``value`` (any JSON) with every ``${user_config.KEY}`` filled in;
    raises :class:`OptionError` for a key with no value."""
    if isinstance(value, str):
        def fill(m: re.Match[str]) -> str:
            if m[1] not in values:
                raise OptionError(f"plugin option {m[1]!r} isn't set")
            return _text(values[m[1]])
        return _REF.sub(fill, value)
    if isinstance(value, Mapping):
        return {k: substitute(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, values) for v in value]
    return value


def uses(value: Any) -> bool:
    """Whether ``value`` (any JSON) names a ``${user_config.KEY}``."""
    return _REF.search(json.dumps(value)) is not None


def content(text: str, values: Mapping[str, Any],
            fields: Mapping[str, Mapping[str, Any]]) -> str:
    """``text`` (a command or agent prompt) with the non-sensitive values
    filled in; a sensitive key gets Claude Code's placeholder, an unset one
    stays as written."""
    def fill(m: re.Match[str]) -> str:
        key = m[1]
        if (fields.get(key) or {}).get("sensitive") is True:
            return f"[sensitive option '{key}' not available in skill content]"
        return _text(values[key]) if key in values else m[0]
    return _REF.sub(fill, text)


def _env_name(key: str) -> str:
    return "CLAUDE_PLUGIN_OPTION_" + re.sub(r"[^A-Za-z0-9_]", "_", key).upper()


def hook_env(values: Mapping[str, Any]) -> dict[str, str]:
    """``CLAUDE_PLUGIN_OPTION_<KEY>`` for each value (a hook's environment;
    a secret stays a reference until the hook is made)."""
    return {_env_name(k): _text(v) for k, v in values.items()}


def names(value: Any, key: str) -> bool:
    """Whether ``value`` (a hook, any JSON) names option ``key``: as
    ``${user_config.KEY}`` or ``CLAUDE_PLUGIN_OPTION_<KEY>``."""
    text = json.dumps(value)
    return f"user_config.{key}}}" in text or _env_name(key) in text


def _parse(key: str, field: Mapping[str, Any], raw: str) -> Any:
    """``raw`` (typed or ``--config``) as the field's value."""
    kind = field.get("type")
    if _SECRET_REF.match(raw.strip()):
        return raw.strip()
    if kind == "boolean":
        word = raw.strip().lower()
        if word in _TRUE or word in _FALSE:
            return word in _TRUE
        raise OptionError(f"{key}: {raw!r} is not a boolean (use true/false, 1/0, yes/no, "
                          "on/off)")
    if kind == "number":
        try:
            number = float(raw)
        except ValueError:
            raise OptionError(f"{key}: {raw!r} is not a number") from None
        if field.get("min") is not None and number < float(field["min"]):
            raise OptionError(f"{key}: {raw} is below its minimum {field['min']}")
        if field.get("max") is not None and number > float(field["max"]):
            raise OptionError(f"{key}: {raw} is above its maximum {field['max']}")
        return int(number) if number.is_integer() else number
    if field.get("multiple") is True:
        return [part.strip() for part in raw.split(",") if part.strip()]
    choices = field.get("options")
    if isinstance(choices, list) and raw not in choices:
        raise OptionError(f"{key}: {raw!r} is not one of {', '.join(map(str, choices))}")
    return raw


def _keep_secret(plugin: str, key: str, value: str) -> str:
    """``value`` in the OS keyring; its reference."""
    try:
        import keyring  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        raise OptionError(f"{key} is sensitive and the keyring package is not installed: "
                          "install the keyring extra, or give the value as {env:NAME}") from None
    try:
        keyring.set_password(KEYRING_SERVICE, f"{plugin}/{key}", value)
    except Exception as exc:  # noqa: BLE001 — NoKeyringError, a locked or broken backend
        raise OptionError(f"{key} is sensitive and this machine's keyring cannot keep it "
                          f"({type(exc).__name__}: {exc}); give it as {{env:NAME}} instead "
                          f"(--config {key}={{env:NAME}}, the variable set where noeta "
                          "runs)") from None
    return f"{{keyring:{KEYRING_SERVICE}/{plugin}/{key}}}"


def configure(plugin: str, fields: Mapping[str, Mapping[str, Any]],
              given: Sequence[str] = (), *, interactive: bool | None = None,
              ask: Callable[[str], str] = input,
              ask_secret: Callable[[str], str] = getpass.getpass) -> list[str]:
    """Set ``plugin``'s values: ``given`` (``KEY=VALUE``), then, on a
    terminal, each one still unset asked for (Enter keeps the default or
    the kept value). Keeps them (see the module docstring); returns notes
    for the person (options still unset)."""
    if given and not fields:
        raise OptionError(f"--config was given but plugin {plugin!r} declares no userConfig "
                          "options")
    values = stored(plugin)
    for pair in given:
        key, eq, raw = pair.partition("=")
        if not eq or not key:
            raise OptionError(f'--config expects KEY=VALUE, got "{pair}"')
        if key not in fields:
            raise OptionError(f"{key!r} isn't declared in this plugin's userConfig")
        if raw == "":
            raise OptionError(f"{key}: value is empty. Omit the flag to leave it unset")
        values[key] = _parse(key, fields[key], raw)
    if interactive is None:
        interactive = sys.stdin.isatty()
    for key, field in fields.items() if interactive else ():
        if key in {p.partition("=")[0] for p in given}:
            continue
        title = str(field.get("title") or key)
        kept = values.get(key, field.get("default"))
        shown = "" if kept is None else " [kept]" if field.get("sensitive") else f" [{_text(kept)}]"
        if field.get("description"):
            print(f"{title}: {field['description']}", file=sys.stderr)
        while True:
            raw = (ask_secret if field.get("sensitive") else ask)(f"{title}{shown}: ")
            if not raw:
                break
            try:
                values[key] = _parse(key, field, raw)
                break
            except OptionError as exc:
                print(f"noeta: {exc}", file=sys.stderr)
    for key, field in fields.items():
        value = values.get(key)
        if field.get("sensitive") and isinstance(value, str) and not _SECRET_REF.match(value):
            values[key] = _keep_secret(plugin, key, value)
        if key not in values and field.get("default") is not None:
            values[key] = field["default"]
    data = _read_settings()
    configs = data.setdefault("pluginConfigs", {})
    if not isinstance(configs, dict):
        raise OptionError(f"{_settings_path()}: pluginConfigs is not an object")
    entry = configs.setdefault(plugin, {})
    entry["options"] = {k: v for k, v in values.items() if k in fields}
    _write_settings(data)
    unset = [k for k in fields if values.get(k) in (None, "")]
    required = [k for k in unset if fields[k].get("required") is True]
    if not unset:
        return []
    return [f"{len(unset)} userConfig option{'s' if len(unset) > 1 else ''} not yet set"
            + (f" ({len(required)} required)" if required else "")
            + f": {', '.join(unset)} — run noeta plugin configure {plugin}, or pass "
              "--config KEY=VALUE"]


def forget(plugin: str) -> None:
    """Drop ``plugin``'s kept values and its keyring secrets."""
    options = stored(plugin)
    for key, value in options.items():
        if isinstance(value, str) and value.startswith(f"{{keyring:{KEYRING_SERVICE}/"):
            try:
                import keyring  # type: ignore[import-not-found, unused-ignore]
                keyring.delete_password(KEYRING_SERVICE, f"{plugin}/{key}")
            except Exception:  # noqa: BLE001 — gone already, or no keyring
                pass
    try:
        data = _read_settings()
    except OptionError:
        return
    configs = data.get("pluginConfigs")
    if isinstance(configs, dict) and plugin in configs:
        del configs[plugin]
        if not configs:
            del data["pluginConfigs"]
        _write_settings(data)
