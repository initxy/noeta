"""``Read`` over a backend with the optional ``read_range`` capability.

A sandbox container used to serve ``Read`` through one whole-file
``read_bytes`` — a 3 MB log was pulled (base64, over HTTP) to show 20 lines,
and a file past the response cap could not be read at all. With ``read_range``
the tool pulls bounded chunks lazily: a small file is still one call, a large
one streams, and nothing else about the result changes (whole body stored up
to 1 MiB, the served window above; digest over every byte).

The second half pins ``AioSandboxExecEnv.read_range``'s wire: a guarded,
byte-exact ``dd`` window through ``/v1/shell/exec``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from noeta.builtins.fs.impl import ReadFileTool
from noeta.builtins.fs.impl.read import _READ_CHUNK_BYTES, _READ_STORE_FULL_MAX
from noeta.builtins.sandbox.impl.exec_env import AioSandboxExecEnv
from noeta.protocols.tool import ToolContext
from noeta.runtime.exec_env import LocalExecEnv
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore


class RangedEnv:
    """A non-local backend (so ``Read`` cannot take the host fast path) that
    serves ``read_range`` from the host disk and refuses ``read_bytes``."""

    def __init__(self) -> None:
        self._local = LocalExecEnv()
        self.ranges: list[tuple[int, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._local, name)

    def read_bytes(self, path: Path) -> bytes:
        raise AssertionError("Read must not pull the whole file")

    def read_range(self, path: Path, offset: int, length: int) -> bytes:
        self.ranges.append((offset, length))
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)


def _setup(tmp_path: Path) -> tuple[ToolContext, WorkspaceRoot, InMemoryContentStore]:
    ws = tmp_path / "ws"
    ws.mkdir()
    store = InMemoryContentStore()
    ctx = ToolContext(
        artifact_store=store, file_read_registry=InMemoryFileReadRegistry()
    )
    return ctx, WorkspaceRoot.from_path(ws), store


def _lines(n: int, width: int = 60) -> bytes:
    return b"".join(
        f"line {i:07d} ".encode() + b"x" * (width - 14) + b"\n" for i in range(n)
    )


def test_small_file_is_one_range_call_and_stores_whole_body(tmp_path: Path) -> None:
    ctx, ws, store = _setup(tmp_path)
    body = _lines(100)
    (ws.root / "small.txt").write_bytes(body)
    env = RangedEnv()
    result = ReadFileTool(workspace=ws, exec_env=env).invoke(
        {"file_path": "small.txt", "offset": 3, "limit": 2}, ctx
    )
    assert result.success
    assert env.ranges == [(0, _READ_STORE_FULL_MAX + 1)]
    assert "line 0000002" in result.output and "line 0000004" not in result.output
    assert store.get(result.artifacts[0]) == body


def test_large_file_streams_in_chunks_and_serves_the_window(tmp_path: Path) -> None:
    ctx, ws, store = _setup(tmp_path)
    body = _lines(60_000)  # ~3.6 MB
    target = ws.root / "big.log"
    target.write_bytes(body)
    env = RangedEnv()
    result = ReadFileTool(workspace=ws, exec_env=env).invoke(
        {"file_path": "big.log", "offset": 50_001, "limit": 3}, ctx
    )
    assert result.success
    assert "line 0050000" in result.output and "line 0050003" not in result.output
    assert "of 60000 total lines" in result.output
    # Bounded pulls, contiguous, covering the whole file.
    assert env.ranges[0] == (0, _READ_STORE_FULL_MAX + 1)
    assert all(length <= _READ_CHUNK_BYTES for _, length in env.ranges[1:])
    offsets = [off for off, _ in env.ranges]
    assert offsets == sorted(offsets) and len(env.ranges) >= 4
    # Above 1 MiB only the served window is stored — the rule is unchanged.
    stored = store.get(result.artifacts[0])
    assert len(stored) < 1000
    # The read-first digest still covers every byte.
    assert ctx.file_read_registry is not None
    assert ctx.file_read_registry.digest(str(target.resolve())) == hashlib.sha256(
        body
    ).hexdigest()


def test_windowed_result_matches_the_local_backend(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    (ws.root / "big.log").write_bytes(_lines(40_000))
    args = {"file_path": "big.log", "offset": 20_000, "limit": 5}
    ranged = ReadFileTool(workspace=ws, exec_env=RangedEnv()).invoke(args, ctx)
    local = ReadFileTool(workspace=ws).invoke(args, ctx)
    assert ranged.output == local.output
    assert ranged.summary == local.summary


def test_offset_past_end_on_a_large_file(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    (ws.root / "big.log").write_bytes(_lines(30_000))
    result = ReadFileTool(workspace=ws, exec_env=RangedEnv()).invoke(
        {"file_path": "big.log", "offset": 99_999}, ctx
    )
    assert result.success
    assert "only 30000 lines" in result.output


def test_exact_probe_size_file_terminates(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    body = b"a" * _READ_STORE_FULL_MAX + b"\n"  # exactly the probe length
    (ws.root / "edge.txt").write_bytes(body)
    env = RangedEnv()
    result = ReadFileTool(workspace=ws, exec_env=env).invoke(
        {"file_path": "edge.txt"}, ctx
    )
    assert result.success
    assert env.ranges[-1][0] == len(body)  # the empty tail read ends it


def test_binary_large_file_stops_early(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    (ws.root / "blob.bin").write_bytes(b"\x00" * (3 * 1024 * 1024))
    env = RangedEnv()
    result = ReadFileTool(workspace=ws, exec_env=env).invoke(
        {"file_path": "blob.bin"}, ctx
    )
    assert result.success is False and "not utf-8 text" in (result.summary or "")
    assert len(env.ranges) == 1


def test_oversize_image_is_refused_without_pulling_it_all(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"\x01" * (9 * 1024 * 1024)
    (ws.root / "huge.png").write_bytes(png)
    env = RangedEnv()
    result = ReadFileTool(workspace=ws, exec_env=env).invoke(
        {"file_path": "huge.png"}, ctx
    )
    assert result.success is False
    assert "inline limit" in (result.summary or "")
    pulled = sum(length for _, length in env.ranges)
    assert pulled < len(png)


def test_image_between_store_limit_and_image_limit_reads(tmp_path: Path) -> None:
    ctx, ws, store = _setup(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"\x02" * (2 * 1024 * 1024)
    (ws.root / "mid.png").write_bytes(png)
    result = ReadFileTool(workspace=ws, exec_env=RangedEnv()).invoke(
        {"file_path": "mid.png"}, ctx
    )
    assert result.success
    assert store.get(result.images[0]) == png


def test_read_range_fault_is_a_read_failure(tmp_path: Path) -> None:
    ctx, ws, _ = _setup(tmp_path)
    (ws.root / "f.txt").write_text("x\n")

    class Denied(RangedEnv):
        def read_range(self, path: Path, offset: int, length: int) -> bytes:
            raise PermissionError("denied")

    result = ReadFileTool(workspace=ws, exec_env=Denied()).invoke(
        {"file_path": "f.txt"}, ctx
    )
    assert result.success is False and "denied" in (result.summary or "")


# -- AioSandboxExecEnv.read_range wire ---------------------------------------- #

BASE = "http://sandbox.local:8080"


class FakeAio:
    def __init__(self, exec_data: dict[str, Any]) -> None:
        self.exec_data = exec_data
        self.commands: list[str] = []

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float
    ) -> bytes:
        assert url == BASE + "/v1/shell/exec"
        self.commands.append(json.loads(body)["command"])
        return json.dumps({"success": True, "data": self.exec_data}).encode()


def test_aio_read_range_sends_a_guarded_dd_window() -> None:
    payload = bytes(range(256)) * 3
    fake = FakeAio(
        {"exit_code": 0, "output": base64.b64encode(payload).decode()}
    )
    env = AioSandboxExecEnv(base_url=BASE, post=fake)
    assert env.read_range(Path("/w/a b.bin"), 4096, 768) == payload
    (cmd,) = fake.commands
    assert "if [ ! -e '/w/a b.bin' ]" in cmd
    assert "dd if='/w/a b.bin'" in cmd
    assert "iflag=skip_bytes,count_bytes skip=4096 count=768" in cmd
    assert cmd.startswith("( ") and cmd.endswith(" )")  # subshell, never bare exit


@pytest.mark.parametrize(
    ("code", "exc"), [(40, FileNotFoundError), (41, PermissionError)]
)
def test_aio_read_range_maps_guard_codes(code: int, exc: type[OSError]) -> None:
    env = AioSandboxExecEnv(
        base_url=BASE, post=FakeAio({"exit_code": code, "output": ""})
    )
    with pytest.raises(exc):
        env.read_range(Path("/w/f"), 0, 10)


def test_aio_read_range_zero_length_makes_no_call() -> None:
    fake = FakeAio({"exit_code": 0, "output": ""})
    env = AioSandboxExecEnv(base_url=BASE, post=fake)
    assert env.read_range(Path("/w/f"), 5, 0) == b""
    assert fake.commands == []
    with pytest.raises(ValueError):
        env.read_range(Path("/w/f"), -1, 5)
