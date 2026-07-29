"""``app`` — thin declaration for the app-preview tool (``open_app``).

The tool is gated on a live host preview gateway (host-wired — ``open_app``
is only ever constructed when the product injects an
:class:`~noeta.runtime.app_preview.AppPreviewGateway`), so the manifest
carries no contributions — declaring it on the identity ``tool`` surface
would merge it into an activating agent's ``AgentSpec``, which the
gateway-gated wiring deliberately does not do. The implementation lives in
this plugin's ``impl`` package (``noeta.builtins.app.impl`` —
``build_app_tools`` / ``OpenAppTool``), resolved by the SDK host through
:func:`noeta.client.parts.default_app_tools_factory` into the kernel
builder's ``app_tools_factory`` injection (microkernel M3).
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="app", requires_noeta=">=0.4")
