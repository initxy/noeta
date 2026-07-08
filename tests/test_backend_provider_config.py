"""The new backend honors the real-provider / write-mode config (noeta.config.json).

Before this, ``BackendConfig.from_env`` read only host/port/workspace/model and
``serve_backend`` always used the offline stub — so ``NOETA_AGENT_CONFIG`` (the
ark / adip config) was silently ignored once the new backend became the default,
and writes never hit disk (``FsWriteMode.DRY_RUN``). Covered here:

* ``from_env`` reads the ``NOETA_AGENT_CONFIG`` JSON (+ env overrides it).
* ``build_provider`` selects the right adapter (and ``stub`` ⇒ ``None``).
* ``write_mode: "apply"`` actually persists a tool write end-to-end (vs the
  ``dry_run`` default, which stages without touching disk).
"""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path

import pytest

from noeta.agent.backend import BackendConfig, serve_backend
from noeta.agent.backend.lifecycle import build_provider
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# config reading
# ---------------------------------------------------------------------------


def test_from_env_reads_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "noeta.config.json"
    cfg.write_text(
        json.dumps(
            {
                "provider_id": "openai-responses",
                "model": "gpt-5.5",
                "base_url": "https://aidp/responses",
                "api_key": "K",
                "api_version": "2024-02-01",
                "write_mode": "apply",
                "workflow_enabled": True,
                "sqlite_path": ":memory:",
                "host": "0.0.0.0",
                "port": 9100,
            }
        )
    )
    c = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert c.provider_id == "openai-responses"
    assert c.model == "gpt-5.5"
    assert c.base_url == "https://aidp/responses"
    assert c.api_key == "K"
    assert c.api_version == "2024-02-01"
    assert c.write_mode == "apply"
    assert c.workflow_enabled is True
    assert c.host == "0.0.0.0" and c.port == 9100


def test_env_overrides_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"provider_id": "openai", "model": "from-file"}))
    c = BackendConfig.from_env(
        {
            "NOETA_AGENT_CONFIG": str(cfg),
            "NOETA_AGENT_MODEL": "from-env",
            "NOETA_AGENT_PROVIDER": "stub",
        }
    )
    assert c.model == "from-env"  # env wins
    assert c.provider_id == "stub"


def test_defaults_are_offline_and_safe() -> None:
    c = BackendConfig.from_env({})
    assert c.provider_id == "stub"
    assert c.write_mode == "dry_run"
    assert c.workflow_enabled is False
    # Single-worker serial default; background_drive on by default for the
    # served product.
    assert c.background_drive is True
    assert c.num_workers == 1
    # Per-session sandbox is OFF by default (needs a local Docker daemon).
    assert c.sandbox_enabled is False


def test_sandbox_config_from_env() -> None:
    c = BackendConfig.from_env(
        {
            "NOETA_AGENT_SANDBOX": "1",
            "NOETA_AGENT_SANDBOX_IMAGE": "ghcr.io/example/sbx:2",
            "NOETA_AGENT_SANDBOX_MEMORY": "4g",
            "NOETA_AGENT_SANDBOX_CPUS": "3",
            "NOETA_AGENT_SANDBOX_API_KEY_ENV": "MY_KEY",
        }
    )
    assert c.sandbox_enabled is True
    assert c.sandbox_image == "ghcr.io/example/sbx:2"
    assert c.sandbox_memory == "4g"
    assert c.sandbox_cpus == "3"
    assert c.sandbox_api_key_env == "MY_KEY"


def test_sandbox_config_from_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps({"sandbox_enabled": True, "sandbox_image": "img:file"}),
        encoding="utf-8",
    )
    c = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert c.sandbox_enabled is True
    assert c.sandbox_image == "img:file"
    # env wins over the file
    c2 = BackendConfig.from_env(
        {"NOETA_AGENT_CONFIG": str(cfg), "NOETA_AGENT_SANDBOX": "0"}
    )
    assert c2.sandbox_enabled is False


def test_num_workers_defaults_to_one() -> None:
    c = BackendConfig.from_env({})
    assert c.num_workers == 1


def test_num_workers_from_env() -> None:
    c = BackendConfig.from_env({"NOETA_AGENT_NUM_WORKERS": "4"})
    assert c.num_workers == 4


def test_num_workers_from_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "noeta.config.json"
    cfg.write_text(json.dumps({"num_workers": 3}))
    c = BackendConfig.from_env({"NOETA_AGENT_CONFIG": str(cfg)})
    assert c.num_workers == 3


def test_num_workers_env_overrides_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"num_workers": 3}))
    c = BackendConfig.from_env(
        {"NOETA_AGENT_CONFIG": str(cfg), "NOETA_AGENT_NUM_WORKERS": "7"}
    )
    assert c.num_workers == 7


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_num_workers_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        BackendConfig.from_env({"NOETA_AGENT_NUM_WORKERS": bad})


# ---------------------------------------------------------------------------
# provider construction
# ---------------------------------------------------------------------------


def test_build_provider_selects_adapter() -> None:
    assert build_provider(BackendConfig(provider_id="stub")) is None
    ark = build_provider(
        BackendConfig(provider_id="openai", base_url="https://ark/v3", api_key="K")
    )
    assert type(ark).__name__ == "OpenAICompatProvider"
    adip = build_provider(
        BackendConfig(
            provider_id="openai-responses",
            base_url="https://aidp/responses",
            api_key="K",
        )
    )
    assert type(adip).__name__ == "OpenAIResponsesProvider"


def test_build_provider_requires_api_key() -> None:
    with pytest.raises(SystemExit):
        build_provider(BackendConfig(provider_id="openai", base_url="https://x"))


# ---------------------------------------------------------------------------
# write_mode end-to-end
# ---------------------------------------------------------------------------


def _writer_provider() -> FakeLLMProvider:
    """Scripted: turn 1 writes out.txt, turn 2 ends."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="w1",
                        tool_name="write",
                        arguments={"path": "out.txt", "content": "persisted"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def _post_goal(host: str, port: int, goal: str) -> str:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(
        "POST",
        "/tasks",
        body=json.dumps({"goal": goal, "permission_mode": "bypassPermissions"}),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return data["task_id"]


def _drain_stream(host: str, port: int, task_id: str, seconds: float = 4.0) -> None:
    """Read the SSE stream briefly so the scripted turns run to completion."""
    conn = http.client.HTTPConnection(host, port, timeout=seconds + 2)
    conn.request("GET", f"/stream?task={task_id}")
    resp = conn.getresponse()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = resp.fp.readline()
        if not line:
            break
        if b"TaskSuspended" in line or b"ConversationClosed" in line:
            break
    conn.close()


def _run_write(tmp_path: Path, write_mode: str) -> bool:
    server, _url, shutdown = serve_backend(
        BackendConfig(
            host="127.0.0.1", port=0, workspace_dir=tmp_path, write_mode=write_mode
        ),
        provider=_writer_provider(),
    )
    host, port = server.server_address[:2]
    try:
        task_id = _post_goal(host, port, "write out.txt")
        _drain_stream(host, port, task_id)
        return (tmp_path / "out.txt").is_file()
    finally:
        shutdown()


def test_write_mode_apply_persists(tmp_path: Path) -> None:
    assert _run_write(tmp_path, "apply") is True
    assert (tmp_path / "out.txt").read_text() == "persisted"


def test_write_mode_dry_run_does_not_persist(tmp_path: Path) -> None:
    assert _run_write(tmp_path, "dry_run") is False
