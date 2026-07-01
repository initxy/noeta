"""``open_app`` tool unit tests (gateway faked).

The tool's contract: validate the dir (inside workspace, has ``index.html``)
+ the proxy target (http/https), register a mount on the injected
:class:`AppPreviewGateway`, and surface the render URL both as ``output`` and as
an ``open_app`` ``side_effect``. A fake gateway records the mount call so these
stay fast + hermetic (the real gateway's HTTP behaviour is covered by
``test_preview_gateway``).
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.tool import ToolContext
from noeta.tools.app import AppMount, OpenAppTool, build_app_tools
from noeta.tools.fs._workspace import WorkspaceRoot


class _FakeGateway:
    """Records mount() calls; returns a deterministic AppMount."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def mount(self, *, workspace_dir: Path, app_rel: str, proxy_to: str, task_id: str) -> AppMount:
        self.calls.append(
            {
                "workspace_dir": workspace_dir,
                "app_rel": app_rel,
                "proxy_to": proxy_to,
                "task_id": task_id,
            }
        )
        return AppMount(token="tok123", url="/preview/tok123/")


def _setup(tmp_path: Path, *, with_index: bool = True) -> tuple[OpenAppTool, _FakeGateway, ToolContext]:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    app_dir = ws_dir / "app"
    app_dir.mkdir()
    if with_index:
        (app_dir / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    workspace = WorkspaceRoot.from_path(ws_dir)
    gw = _FakeGateway()
    tool = OpenAppTool(workspace=workspace, gateway=gw)
    ctx = ToolContext(artifact_store=None, metadata={"task_id": "T1"})
    return tool, gw, ctx


def test_metadata(tmp_path: Path) -> None:
    tool, _, _ = _setup(tmp_path)
    assert tool.name == "open_app"
    assert tool.risk_level == "low"
    assert tool.description  # loaded from descriptions/open_app.md
    assert tool.input_schema["required"] == ["dir", "proxy_to"]


def test_success_mounts_and_signals(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "app", "proxy_to": "http://localhost:3000"}, ctx)
    assert result.success is True
    assert result.output["url"] == "/preview/tok123/"
    assert result.output["path"] == "app"
    assert result.output["proxy_to"] == "http://localhost:3000"
    # the open_app side-effect is the frontend's signal to open the panel
    assert result.side_effects == [
        {"type": "open_app", "url": "/preview/tok123/", "dir": "app"}
    ]
    # gateway got the workspace root + relative app dir + target + task id
    assert len(gw.calls) == 1
    call = gw.calls[0]
    assert call["app_rel"] == "app"
    assert call["proxy_to"] == "http://localhost:3000"
    assert call["task_id"] == "T1"
    assert Path(call["workspace_dir"]).name == "ws"


def test_https_proxy_ok(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "app", "proxy_to": "https://api.example.com"}, ctx)
    assert result.success is True
    assert len(gw.calls) == 1


def test_missing_dir_arg(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"proxy_to": "http://x"}, ctx)
    assert result.success is False
    assert "dir" in result.summary
    assert gw.calls == []


def test_missing_proxy_to_arg(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "app"}, ctx)
    assert result.success is False
    assert "proxy_to" in result.summary
    assert gw.calls == []


def test_non_http_proxy_rejected(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "app", "proxy_to": "ftp://nope"}, ctx)
    assert result.success is False
    assert "http(s)" in result.summary
    assert gw.calls == []


def test_not_a_directory(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "app/index.html", "proxy_to": "http://x"}, ctx)
    assert result.success is False
    assert "not a directory" in result.summary
    assert gw.calls == []


def test_missing_index_html(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path, with_index=False)
    result = tool.invoke({"dir": "app", "proxy_to": "http://x"}, ctx)
    assert result.success is False
    assert "index.html" in result.summary
    assert gw.calls == []


def test_workspace_escape_rejected(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)
    result = tool.invoke({"dir": "../outside", "proxy_to": "http://x"}, ctx)
    assert result.success is False
    assert "outside workspace" in result.summary
    assert gw.calls == []


def test_gateway_failure_degrades(tmp_path: Path) -> None:
    tool, gw, ctx = _setup(tmp_path)

    def _boom(**_kwargs):
        raise RuntimeError("port exhausted")

    gw.mount = _boom  # type: ignore[assignment]
    result = tool.invoke({"dir": "app", "proxy_to": "http://x"}, ctx)
    assert result.success is False
    assert "gateway mount failed" in result.summary


def test_build_app_tools_pack(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    workspace = WorkspaceRoot.from_path(ws_dir)
    pack = build_app_tools(workspace, _FakeGateway())
    assert set(pack) == {"open_app"}
    assert isinstance(pack["open_app"], OpenAppTool)
