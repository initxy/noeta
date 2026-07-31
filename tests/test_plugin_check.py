"""Tests for the manifest packaging verifier (``python -m noeta.sdk.plugin_check``).

Spec D1: a single-file plugin's decorators are the source of truth; a distributed
plugin must *also* ship a static manifest the loader reads without importing code.
``plugin_check`` derives the manifest from the decorators and verifies the shipped
static manifest matches. Covered here: deriving from a builder, locating the
shipped manifest (``noeta-plugin.toml`` and ``pyproject.toml [tool.noeta]``), the
normalized diff (match + every drift class), TOML emission round-tripping back to
the same manifest, the per-plugin check over the real example corpus, and the CLI
(verify + ``--emit``) including a ``python -m`` subprocess smoke.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from noeta.client.plugin_manifest import ManifestContribution, PluginManifest, parse_manifest_text
from noeta.sdk.plugin_check import (
    check_plugin,
    derive_manifest,
    diff_manifests,
    find_shipped_manifest,
    main,
    manifest_to_toml,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "plugins"
_ALL_EXAMPLES = (
    "protected-paths",
    "git-checkpoint",
    "approval-modes",
    "redaction",
    "checklist-reminder",
    "memory-recall",
)


def _write_plugin(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


_SIMPLE_PLUGIN = """
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("sample", requires_noeta=">=0.4")

    def render(view):  # pragma: no cover — identity only
        return None

    plugin.reminder(render, name="nudge", priority=42)
"""


# ---------------------------------------------------------------------------
# Deriving + locating manifests
# ---------------------------------------------------------------------------


def test_derive_manifest_from_builder(tmp_path):
    path = _write_plugin(tmp_path, _SIMPLE_PLUGIN)
    m = derive_manifest(path)
    assert m.name == "sample" and m.requires_noeta == ">=0.4"
    (c,) = m.contributions
    assert (c.surface, c.name, c.params.get("priority")) == ("reminder", "nudge", 42)


def test_find_shipped_manifest_prefers_noeta_plugin_toml(tmp_path):
    (tmp_path / "noeta-plugin.toml").write_text('name = "sample"\n', encoding="utf-8")
    found = find_shipped_manifest(tmp_path)
    assert found is not None
    manifest, source = found
    assert manifest.name == "sample" and source.name == "noeta-plugin.toml"


def test_find_shipped_manifest_reads_pyproject_tool_noeta(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.noeta]\nname = "fromproj"\n', encoding="utf-8"
    )
    found = find_shipped_manifest(tmp_path)
    assert found is not None and found[0].name == "fromproj"


def test_find_shipped_manifest_none_when_absent(tmp_path):
    assert find_shipped_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# The normalized diff — match + every drift class
# ---------------------------------------------------------------------------


def _m(*contribs: ManifestContribution, name="p", requires=">=0.4") -> PluginManifest:
    return PluginManifest(name=name, requires_noeta=requires, contributions=contribs)


def test_diff_is_empty_when_ref_module_differs_but_attribute_matches():
    # The module portion of a ref is an install-layout concern — normalized away.
    derived = _m(ManifestContribution("guard", "g", ref="_loaded_module:GUARD"))
    shipped = _m(ManifestContribution("guard", "g", ref="pkg:GUARD"))
    assert diff_manifests(derived, shipped) == []


def test_diff_flags_name_and_requires():
    derived = _m(name="a", requires=">=0.4")
    shipped = _m(name="b", requires=">=0.5")
    diffs = diff_manifests(derived, shipped)
    assert any("name:" in d for d in diffs)
    assert any("requires-noeta:" in d for d in diffs)


def test_diff_flags_missing_and_extra_contribution():
    derived = _m(ManifestContribution("reminder", "x", ref="m:x"))
    shipped = _m(ManifestContribution("reminder", "y", ref="m:y"))
    diffs = diff_manifests(derived, shipped)
    assert any("('reminder', 'x')" in d and "not shipped" in d for d in diffs)
    assert any("('reminder', 'y')" in d and "not declared" in d for d in diffs)


def test_diff_flags_param_and_ref_attribute_drift():
    derived = _m(ManifestContribution("reminder", "x", ref="m:foo", params={"priority": 1}))
    shipped = _m(ManifestContribution("reminder", "x", ref="m:bar", params={"priority": 2}))
    diffs = diff_manifests(derived, shipped)
    assert any("ref_attr" in d for d in diffs)
    assert any("params" in d for d in diffs)


def test_diff_normalizes_list_vs_tuple_params():
    derived = _m(ManifestContribution("reminder_provider", "r", ref="m:r", params={"seams": ("turn_intake",)}))
    shipped = _m(ManifestContribution("reminder_provider", "r", ref="m:r", params={"seams": ["turn_intake"]}))
    assert diff_manifests(derived, shipped) == []


# ---------------------------------------------------------------------------
# TOML emission round-trips back to the same manifest
# ---------------------------------------------------------------------------


def test_manifest_to_toml_round_trips(tmp_path):
    derived = derive_manifest(_write_plugin(tmp_path, _SIMPLE_PLUGIN))
    reparsed = parse_manifest_text(manifest_to_toml(derived))
    assert diff_manifests(derived, reparsed) == []


def test_manifest_to_toml_emits_seams_and_config_schema():
    manifest = PluginManifest(
        name="x",
        requires_noeta=">=0.4",
        config_schema={"env": {"KEY": "desc"}},
        contributions=(
            ManifestContribution("reminder_provider", "r", ref="m:r", params={"seams": ["turn_intake"]}),
        ),
    )
    toml = manifest_to_toml(manifest)
    assert "[config-schema.env]" in toml
    assert 'seams = ["turn_intake"]' in toml
    assert parse_manifest_text(toml).name == "x"


# ---------------------------------------------------------------------------
# check_plugin over the real example corpus + crafted drift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _ALL_EXAMPLES)
def test_every_example_plugin_matches_its_shipped_manifest(name):
    result = check_plugin(EXAMPLES / name)
    assert result.ok, "\n".join(result.messages)


def test_check_plugin_accepts_a_file_or_a_directory():
    by_dir = check_plugin(EXAMPLES / "redaction")
    by_file = check_plugin(EXAMPLES / "redaction" / "plugin.py")
    assert by_dir.ok and by_file.ok


def test_check_plugin_fails_on_drift(tmp_path):
    _write_plugin(tmp_path, _SIMPLE_PLUGIN)  # declares priority 42
    (tmp_path / "noeta-plugin.toml").write_text(
        'name = "sample"\nrequires-noeta = ">=0.4"\n'
        '[[contributions]]\nsurface = "reminder"\nname = "nudge"\nref = "m:render"\n'
        "priority = 999\n",  # drift: wrong priority
        encoding="utf-8",
    )
    result = check_plugin(tmp_path)
    assert not result.ok
    assert any("params" in m for m in result.messages)


def test_check_plugin_fails_when_no_shipped_manifest(tmp_path):
    _write_plugin(tmp_path, _SIMPLE_PLUGIN)  # no noeta-plugin.toml / pyproject
    result = check_plugin(tmp_path)
    assert not result.ok
    assert any("no shipped manifest" in m for m in result.messages)


def test_check_plugin_fails_when_no_plugin_py(tmp_path):
    result = check_plugin(tmp_path)
    assert not result.ok


@pytest.mark.parametrize(
    ("shipped", "why"),
    [
        ("name = \nnot toml at all [[[", "not valid TOML"),
        ('requires-noeta = ">=0.4"\n', "missing a string 'name'"),
    ],
)
def test_check_plugin_reports_a_malformed_shipped_manifest(tmp_path, shipped, why):
    """A broken shipped manifest is a FINDING, not a crash.

    Reading it raises ``PluginError`` from deep inside the parser. Letting that
    escape aborts the whole run with a traceback and skips every remaining PATH —
    the opposite of what a publish-time verifier is for.
    """
    _write_plugin(tmp_path, _SIMPLE_PLUGIN)
    (tmp_path / "noeta-plugin.toml").write_text(shipped, encoding="utf-8")

    result = check_plugin(tmp_path)
    assert not result.ok
    assert any(why in m for m in result.messages), result.messages


def test_main_keeps_going_after_a_malformed_manifest(tmp_path, capsys):
    """One bad path must not take the rest of the run down with it."""
    bad = tmp_path / "bad"
    _write_plugin(bad, _SIMPLE_PLUGIN)
    (bad / "noeta-plugin.toml").write_text("[[[", encoding="utf-8")

    code = main([str(bad), str(EXAMPLES / "redaction")])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "OK" in out


# ---------------------------------------------------------------------------
# A literal-valued contribution survives --emit (there is nothing to import)
# ---------------------------------------------------------------------------


_FRAGMENT_PLUGIN = """
    from noeta.sdk import PluginBuilder

    plugin = PluginBuilder("styled", requires_noeta=">=0.4")
    plugin.prompt_fragment("Follow the house style.\\n\\nUse \\"quotes\\" sparingly.",
                           name="style")
"""


def test_prompt_fragment_text_survives_emission(tmp_path):
    """A ``prompt_fragment`` carries a literal, not a ref — so the TOML must hold it.

    Emitting ``surface``/``name`` alone produced a manifest that verified clean
    (both sides had no ref) and then raised at resolve time in a real install,
    where there is no builder cache to fall back on.
    """
    derived = derive_manifest(_write_plugin(tmp_path, _FRAGMENT_PLUGIN))
    toml = manifest_to_toml(derived)
    reparsed = parse_manifest_text(toml)

    assert diff_manifests(derived, reparsed) == []
    (contribution,) = reparsed.contributions
    assert contribution.params["text"] == (
        'Follow the house style.\n\nUse "quotes" sparingly.'
    )


def test_an_emitted_fragment_resolves_from_the_static_manifest_alone(tmp_path):
    """The end the emission exists for: a package install with no builder cache."""
    from noeta.client.plugin_set import load_plugins

    derived = derive_manifest(_write_plugin(tmp_path / "src", _FRAGMENT_PLUGIN))
    shipped = tmp_path / "pkg"
    shipped.mkdir()
    (shipped / "noeta-plugin.toml").write_text(
        manifest_to_toml(derived), encoding="utf-8"
    )

    plugins = load_plugins(
        builtins=False, modules=[str(shipped / "noeta-plugin.toml")]
    )
    activation = plugins.identity_activations()["styled"]
    assert activation.prompt_fragments == (
        ("style", 'Follow the house style.\n\nUse "quotes" sparingly.'),
    )


# ---------------------------------------------------------------------------
# The CLI: verify (exit codes) + --emit
# ---------------------------------------------------------------------------


def test_main_verify_returns_zero_for_the_corpus():
    argv = [str(EXAMPLES / name) for name in _ALL_EXAMPLES]
    assert main(argv) == 0


def test_main_verify_returns_one_on_drift(tmp_path, capsys):
    _write_plugin(tmp_path, _SIMPLE_PLUGIN)  # no shipped manifest → fail
    assert main([str(tmp_path)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_emit_prints_derived_toml(tmp_path, capsys):
    path = _write_plugin(tmp_path, _SIMPLE_PLUGIN)
    assert main(["--emit", str(path)]) == 0
    out = capsys.readouterr().out
    assert 'name = "sample"' in out
    assert 'surface = "reminder"' in out and "priority = 42" in out


def test_main_emit_multiple_and_error_path(tmp_path, capsys):
    good = _write_plugin(tmp_path / "good", _SIMPLE_PLUGIN)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "plugin.py").write_text("x = 1\n", encoding="utf-8")  # no builder
    rc = main(["--emit", str(good), str(tmp_path / "bad" / "plugin.py")])
    captured = capsys.readouterr()
    assert rc == 1  # the bad one fails the run
    assert 'name = "sample"' in captured.out
    assert "error:" in captured.err


def test_main_verify_prints_diff_detail_lines(tmp_path, capsys):
    _write_plugin(tmp_path, _SIMPLE_PLUGIN)
    (tmp_path / "noeta-plugin.toml").write_text(
        'name = "sample"\nrequires-noeta = ">=0.4"\n'
        '[[contributions]]\nsurface = "reminder"\nname = "nudge"\nref = "m:render"\n'
        "priority = 999\n",
        encoding="utf-8",
    )
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "does not match" in out  # header + a detail line


# ---------------------------------------------------------------------------
# Deriving errors + shipped-manifest edge cases
# ---------------------------------------------------------------------------


def test_derive_manifest_no_builder_raises(tmp_path):
    from noeta.client.plugins import PluginError

    path = _write_plugin(tmp_path, "x = 1\n")
    with pytest.raises(PluginError, match="PluginBuilder"):
        derive_manifest(path)


def test_derive_manifest_multiple_builders_raises(tmp_path):
    from noeta.client.plugins import PluginError

    path = _write_plugin(
        tmp_path,
        """
        from noeta.sdk import PluginBuilder
        one = PluginBuilder("one")
        two = PluginBuilder("two")
        """,
    )
    with pytest.raises(PluginError, match="multiple PluginBuilder"):
        derive_manifest(path)


def test_check_plugin_reports_a_broken_plugin_file(tmp_path):
    _write_plugin(tmp_path, "import nonexistent_xyz_module\n")
    result = check_plugin(tmp_path)
    assert not result.ok


def test_find_shipped_manifest_ignores_pyproject_without_tool_noeta(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert find_shipped_manifest(tmp_path) is None


def test_runs_as_python_dash_m_module():
    """The verifier is invoked only as ``python -m noeta.sdk.plugin_check`` (no
    console script, per CONTEXT.md)."""
    proc = subprocess.run(
        [sys.executable, "-m", "noeta.sdk.plugin_check", str(EXAMPLES / "redaction")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# D12c — default-valued ordering params are not drift
# ---------------------------------------------------------------------------


def test_omitting_a_defaulted_param_is_not_reported_as_drift():
    """A TOML that omits ``priority = 0`` behaves identically — so it must verify.

    The decorators always stamp the explicit default (``priority=0`` /
    ``seams=()``), while a hand-written manifest naturally leaves it out. The
    loader defaults both the same way (``plugin_set._priority`` / ``_seams``),
    so reporting the pair as a mismatch flagged two manifests that produce
    byte-identical runtime wiring.
    """
    derived = PluginManifest(
        name="p",
        contributions=(
            ManifestContribution(
                surface="reminder", name="r", ref="pkg.mod:r", params={"priority": 0}
            ),
            ManifestContribution(
                surface="reminder_provider", name="s", ref="pkg.mod:s",
                params={"seams": []},
            ),
        ),
    )
    shipped = PluginManifest(
        name="p",
        contributions=(
            ManifestContribution(surface="reminder", name="r", ref="pkg.mod:r"),
            ManifestContribution(
                surface="reminder_provider", name="s", ref="pkg.mod:s"
            ),
        ),
    )
    assert diff_manifests(derived, shipped) == []


def test_a_non_default_ordering_param_is_still_drift():
    """The tolerance is for the DEFAULT value only — a real difference still fails."""
    derived = PluginManifest(
        name="p",
        contributions=(
            ManifestContribution(
                surface="reminder", name="r", ref="pkg.mod:r", params={"priority": 50}
            ),
        ),
    )
    shipped = PluginManifest(
        name="p",
        contributions=(
            ManifestContribution(surface="reminder", name="r", ref="pkg.mod:r"),
        ),
    )
    diffs = diff_manifests(derived, shipped)
    assert len(diffs) == 1 and "params" in diffs[0]


# ---------------------------------------------------------------------------
# D12b — (surface, name) uniqueness, matching PluginBuilder's rule
# ---------------------------------------------------------------------------


def test_toml_manifest_rejects_a_duplicate_surface_name_pair():
    """The static form now refuses what ``PluginBuilder.contribute`` always did.

    Before this, a *distributed* plugin could ship two identically-keyed
    contributions (the second silently shadowing the first in every by-key
    projection) while the same plugin's single-file form was rejected.
    """
    text = """
        name = "dup"
        [[contributions]]
        surface = "reminder"
        name = "same"
        ref = "pkg.mod:one"
        [[contributions]]
        surface = "reminder"
        name = "same"
        ref = "pkg.mod:two"
    """
    with pytest.raises(Exception, match="must be unique"):
        parse_manifest_text(textwrap.dedent(text), origin="dup-manifest")
