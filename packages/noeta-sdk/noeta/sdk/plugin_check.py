"""``python -m noeta.sdk.plugin_check`` — the manifest packaging verifier.

A distributed plugin must ship a static manifest (``[tool.noeta]`` in
``pyproject.toml``, mirrored to ``noeta-plugin.toml`` package data) alongside
the decorator sugar that declares its contributions, so the loader can read the
plugin's identity and contributions **without importing any plugin code**; this
tool is the publish-time check that the two agree. Deliberately not a console
script — there is no operator CLI::

    python -m noeta.sdk.plugin_check PATH [PATH ...]     # verify (default)
    python -m noeta.sdk.plugin_check --emit PATH         # print derived TOML

``PATH`` is a single-file plugin (``plugin.py``) or a directory containing one.
Comparison normalizes the module portion of a ``ref`` away — it is an
install-layout concern resolved at import time — and treats ``config-schema`` as
advisory. Exit status is ``0`` when every path is consistent, ``1`` otherwise.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from noeta.client.plugin_manifest import (
    MANIFEST_BASENAME,
    ManifestContribution,
    PluginManifest,
    exec_plugin_file,
    find_builder,
    parse_manifest_text,
    read_manifest_file,
    ref_attr_name,
)
from noeta.client.plugins import PluginError


__all__ = [
    "derive_manifest",
    "find_shipped_manifest",
    "diff_manifests",
    "manifest_to_toml",
    "check_plugin",
    "main",
]


def derive_manifest(plugin_py: Path) -> PluginManifest:
    """Execute ``plugin_py`` and return the manifest its ``PluginBuilder`` declares.

    The one step that runs plugin code, acceptable because ``plugin_check`` is an
    author-side publish tool over a trusted local file — the same trust class the
    single-file loader assumes. A missing or duplicate ``PluginBuilder`` raises
    :class:`PluginError`, naming the file.
    """
    module = exec_plugin_file(plugin_py, module_prefix="_noeta_plugin_check_")
    builder = find_builder(module, plugin_py)
    return builder.manifest()


def find_shipped_manifest(directory: Path) -> Optional[tuple[PluginManifest, Path]]:
    """The distributed manifest shipped beside a plugin, or ``None``.

    ``noeta-plugin.toml`` — the wheel package-data form, and what an installed
    plugin actually carries — wins over a ``pyproject.toml`` ``[tool.noeta]``.
    """
    packaged = directory / MANIFEST_BASENAME
    if packaged.is_file():
        return read_manifest_file(packaged), packaged
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if "[tool.noeta]" in text:
            return parse_manifest_text(text, origin=f"manifest {str(pyproject)!r}"), pyproject
    return None


# ---------------------------------------------------------------------------
# Comparison — normalized so an install-layout ``ref`` module never fails the
# check, and so an omitted param never differs from its explicit default.
# ---------------------------------------------------------------------------


def _norm_scalar(value: Any) -> Any:
    return list(value) if isinstance(value, (list, tuple)) else value


#: Ordering / binding params the **loader** defaults when a manifest omits them,
#: paired with the value that omission means. A hand-written
#: ``noeta-plugin.toml`` that leaves ``priority`` out has identical runtime
#: effect to one writing ``priority = 0``, yet the decorators always stamp the
#: explicit value — comparing raw would report drift between two manifests that
#: behave the same. Normalizing to "absent" on BOTH sides keeps the verifier
#: honest about what actually differs.
#:
#: These values mirror ``plugin_set._priority`` / ``plugin_set._seams``; a change
#: there must be reflected here (the pair is small and pinned by a test).
_DEFAULTED_PARAMS: Mapping[str, Any] = {
    "priority": 0,
    "seams": [],
}


def _norm_params(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        normalized = _norm_scalar(value)
        if key in _DEFAULTED_PARAMS and normalized == _DEFAULTED_PARAMS[key]:
            continue  # an explicit default is the same manifest as an omission
        out[key] = normalized
    return out


def _norm_contribution(c: ManifestContribution) -> dict[str, Any]:
    return {
        "surface": c.surface,
        "name": c.name,
        "ref_attr": ref_attr_name(c.ref),
        "path": c.path,
        "params": _norm_params(c.params),
    }


def diff_manifests(derived: PluginManifest, shipped: PluginManifest) -> list[str]:
    """Human-readable differences between a derived and a shipped manifest.

    Empty ⇒ consistent. ``name`` / ``requires-noeta`` are compared exactly,
    contributions by ``(surface, name)`` with a normalized ``ref`` attribute;
    ``config-schema`` is advisory and not compared at all.
    """
    diffs: list[str] = []
    if derived.name != shipped.name:
        diffs.append(f"name: decorators={derived.name!r} shipped={shipped.name!r}")
    if derived.requires_noeta != shipped.requires_noeta:
        diffs.append(
            f"requires-noeta: decorators={derived.requires_noeta!r} "
            f"shipped={shipped.requires_noeta!r}"
        )

    d_map = {(c.surface, c.name): _norm_contribution(c) for c in derived.contributions}
    s_map = {(c.surface, c.name): _norm_contribution(c) for c in shipped.contributions}

    for key in sorted(d_map.keys() - s_map.keys()):
        diffs.append(f"contribution {key} is declared by decorators but not shipped")
    for key in sorted(s_map.keys() - d_map.keys()):
        diffs.append(f"contribution {key} is shipped but not declared by decorators")
    for key in sorted(d_map.keys() & s_map.keys()):
        dc, sc = d_map[key], s_map[key]
        for field in ("ref_attr", "path", "params"):
            if dc[field] != sc[field]:
                diffs.append(
                    f"contribution {key} {field}: decorators={dc[field]!r} "
                    f"shipped={sc[field]!r}"
                )
    return diffs


# ---------------------------------------------------------------------------
# Emitting canonical TOML, so an author generates the manifest to ship rather
# than hand-writing one that then drifts.
# ---------------------------------------------------------------------------


#: Characters a TOML *basic string* cannot carry literally. A literal-valued
#: contribution (a ``prompt_fragment``'s text) is routinely multi-line, so
#: emitting these raw would produce a manifest that does not parse.
_TOML_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    text = str(value)
    for raw, escaped in _TOML_ESCAPES:
        text = text.replace(raw, escaped)
    return '"' + text + '"'


def manifest_to_toml(manifest: PluginManifest) -> str:
    """Render ``manifest`` as the canonical ``noeta-plugin.toml`` text.

    Deterministic, so re-emitting an unchanged plugin produces byte-identical
    output: contributions keep their declared order and each table's params are
    sorted by key.

    A contribution whose value is a **literal** rather than an importable object
    (``prompt_fragment``) carries that literal as an ordinary param
    (:data:`~noeta.client.plugin_manifest.LITERAL_PARAM`) so it round-trips
    through this emitter — an installed package has no builder cache to fall back
    on and nothing to import.
    """
    lines = [f"name = {_toml_scalar(manifest.name)}"]
    if manifest.requires_noeta is not None:
        lines.append(f"requires-noeta = {_toml_scalar(manifest.requires_noeta)}")
    if manifest.config_schema:
        for section, body in manifest.config_schema.items():
            if isinstance(body, Mapping):
                lines += ["", f"[config-schema.{section}]"]
                for k, v in body.items():
                    lines.append(f"{k} = {_toml_scalar(v)}")
    for c in manifest.contributions:
        lines += ["", "[[contributions]]", f"surface = {_toml_scalar(c.surface)}"]
        lines.append(f"name = {_toml_scalar(c.name)}")
        if c.ref is not None:
            lines.append(f"ref = {_toml_scalar(c.ref)}")
        if c.path is not None:
            lines.append(f"path = {_toml_scalar(c.path)}")
        for k in sorted(c.params):
            lines.append(f"{k} = {_toml_scalar(c.params[k])}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Per-plugin check + CLI.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    plugin_py: Path
    ok: bool
    messages: tuple[str, ...] = ()


def _resolve_plugin_py(path: Path) -> Path:
    if path.is_dir():
        return path / "plugin.py"
    return path


def check_plugin(path: Path) -> CheckResult:
    """Verify a plugin's shipped manifest matches its decorators."""
    plugin_py = _resolve_plugin_py(path)
    if not plugin_py.is_file():
        return CheckResult(plugin_py, False, (f"no plugin.py at {plugin_py}",))
    # Every PluginError from here on is a FINDING about the plugin under test —
    # a malformed shipped manifest is exactly what this tool exists to report —
    # so both halves are caught. Letting one escape would abort the whole run
    # with a traceback and skip every remaining PATH.
    try:
        derived = derive_manifest(plugin_py)
        shipped = find_shipped_manifest(plugin_py.parent)
    except PluginError as exc:
        return CheckResult(plugin_py, False, (str(exc),))

    if shipped is None:
        return CheckResult(
            plugin_py,
            False,
            (
                f"no shipped manifest beside {plugin_py.name} — ship a "
                f"{MANIFEST_BASENAME} or a pyproject.toml [tool.noeta] "
                f"(run with --emit to generate it)",
            ),
        )
    shipped_manifest, shipped_path = shipped
    diffs = diff_manifests(derived, shipped_manifest)
    if diffs:
        return CheckResult(
            plugin_py,
            False,
            (f"{shipped_path} does not match the decorators:", *(f"  - {d}" for d in diffs)),
        )
    return CheckResult(plugin_py, True, (f"matches {shipped_path.name}",))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry: verify (default) or ``--emit`` the derived TOML."""
    parser = argparse.ArgumentParser(
        prog="python -m noeta.sdk.plugin_check",
        description="Verify a single-file plugin's shipped manifest matches its "
        "decorators.",
    )
    parser.add_argument(
        "paths", nargs="+", metavar="PATH",
        help="a plugin.py, or a directory containing one",
    )
    parser.add_argument(
        "--emit", action="store_true",
        help="print the manifest derived from the decorators (TOML) instead of "
        "verifying",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.emit:
        failed = False
        for raw in args.paths:
            plugin_py = _resolve_plugin_py(Path(raw))
            try:
                manifest = derive_manifest(plugin_py)
            except PluginError as exc:
                print(f"error: {exc}", file=sys.stderr)
                failed = True
                continue
            print(f"# derived from {plugin_py}")
            print(manifest_to_toml(manifest), end="")
        return 1 if failed else 0

    any_failed = False
    for raw in args.paths:
        result = check_plugin(Path(raw))
        marker = "OK  " if result.ok else "FAIL"
        head, *rest = result.messages or ("",)
        print(f"{marker} {result.plugin_py}: {head}")
        for line in rest:
            print(f"     {line}")
        any_failed = any_failed or not result.ok
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
