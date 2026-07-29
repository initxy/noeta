"""``browser`` — thin declaration for the sandbox browser tool pack.

Browser tools are gated on a live sandbox backend (host-wired), so the
manifest carries no contributions — declaring them on the identity ``tool``
surface would merge them into an activating agent's ``AgentSpec``, which the
capability flag deliberately does not do; it exists so every built-in
capability has a catalogue entry and the activation name resolves. The pack
implementation lives in this plugin's ``impl`` package (microkernel M3:
``noeta.builtins.browser.impl`` — ``build_browser_tools`` +
``BROWSER_TOOL_NAMES``), resolved by the SDK host through
:func:`noeta.client.parts.default_browser_tools_factory` into the kernel
builder's ``browser_tools_factory`` injection.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="browser", requires_noeta=">=0.4")
