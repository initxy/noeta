"""Dynamic-config allowlist registry + read helpers.

Design constraints:
- Only settings that are re-read at every use — so a change takes effect
  immediately — belong here. Settings that need a restart stay out of the
  allowlist, avoiding "I changed it and nothing happened" fake dynamism.
- Reads go through resolve_config: a DB override wins, otherwise the static
  Settings value applies.
- PUT accepts only registered keys; values are validated by the item's coerce.

Adding an item requires switching its read sites to config_value(...) first;
do not register a key whose readers still cache the static value.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Request

from noeta.agent.config import Settings
from noeta.agent.store.app_config import AppConfigStore


@dataclass(frozen=True)
class ConfigItem:
    key: str
    # Settings attribute that provides the fallback (the static value used
    # when no DB override exists).
    settings_attr: str
    type: str  # "bool" (the only supported type today; extend coerce with it)
    description: str
    # Validates external input (the PUT body's value) and converts it to the
    # stored form; raises ValueError on invalid input.
    coerce: Callable[[Any], Any]


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    raise ValueError("must be a boolean")


CONFIG_REGISTRY: dict[str, ConfigItem] = {
    "dev_login_enabled": ConfigItem(
        key="dev_login_enabled",
        settings_attr="dev_login_enabled",
        type="bool",
        description="Whether dev-login (development-mode login) is allowed.",
        coerce=_coerce_bool,
    ),
}


def resolve_config(store: AppConfigStore, settings: Settings, key: str) -> Any:
    """The currently effective value of a registered item: a DB override
    wins, otherwise the static Settings value."""
    item = CONFIG_REGISTRY[key]
    override = store.get(key)
    if override is not None:
        return override
    return getattr(settings, item.settings_attr)


def config_value(request: Request, key: str) -> Any:
    """Read a registered item's effective value in request context (the
    single entry point for read sites)."""
    return resolve_config(
        request.app.state.app_config_store, request.app.state.settings, key
    )


def list_config(store: AppConfigStore, settings: Settings) -> list[dict]:
    """Current view of every registered item (for the admin config page):
    default / effective value / override flag + metadata."""
    out: list[dict] = []
    for item in CONFIG_REGISTRY.values():
        meta = store.get_meta(item.key)
        default = getattr(settings, item.settings_attr)
        value = meta["value"] if meta is not None else default
        out.append(
            {
                "key": item.key,
                "type": item.type,
                "description": item.description,
                "value": value,
                "default": default,
                "overridden": meta is not None,
                "updated_by": meta["updated_by"] if meta else None,
                "updated_at": meta["updated_at"] if meta else None,
            }
        )
    return out


def coerce_config(key: str, value: Any) -> Any:
    """Validate and convert a PUT value; raises KeyError for unregistered
    keys and ValueError for invalid values."""
    return CONFIG_REGISTRY[key].coerce(value)
