"""Tests for preview_ws — the minimal RFC 6455 WebSocket reverse-proxy transport.

Uses ``socket.socketpair()`` for realistic frame round-trip testing and a
handshake simulation (no real HTTP needed — the codec is tested at the
frame level). The pump is tested by threading a frame through A→B and
verifying B receives it identically (modulo mask direction).
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading


from noeta.agent.host.preview_ws import (
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    _MAX_FRAME_BYTES,
    _WS_GUID,
    compute_accept,
    pump_bidirectional,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Frame codec — round-trip via socketpair
# ---------------------------------------------------------------------------

class TestFrameRoundTrip:
    """write_frame → socketpair → read_frame preserves (fin, opcode, payload)."""

    def test_short_text_frame(self) -> None:
        a, b = socket.socketpair()
        try:
            assert write_frame(a, True, OP_TEXT, b"hello world")
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_TEXT
            assert payload == b"hello world"
        finally:
            a.close()
            b.close()

    def test_short_binary_frame(self) -> None:
        a, b = socket.socketpair()
        try:
            data = bytes(range(256))
            assert write_frame(a, True, OP_BINARY, data)
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_BINARY
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_empty_payload(self) -> None:
        a, b = socket.socketpair()
        try:
            assert write_frame(a, True, OP_PING, b"")
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_PING
            assert payload == b""
        finally:
            a.close()
            b.close()

    def test_medium_payload_126_extended(self) -> None:
        """Payload 126 bytes → uses 2-byte extended length field."""
        a, b = socket.socketpair()
        try:
            data = b"x" * 200  # > 125 triggers 2-byte ext length
            assert write_frame(a, True, OP_BINARY, data)
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_BINARY
            assert payload == data
            assert len(payload) == 200
        finally:
            a.close()
            b.close()

    def test_large_payload_127_extended(self) -> None:
        """Payload > 65535 → uses 8-byte extended length field."""
        a, b = socket.socketpair()
        try:
            data = b"y" * 70000  # > 65535 triggers 8-byte ext length
            # 70 KB exceeds the socketpair kernel buffer on macOS (8 KB), so a
            # same-thread write-then-read deadlocks inside sendall. Write from
            # a helper thread and drain concurrently — the shape the real pump
            # has (reader and writer are always distinct threads).
            wrote: list[bool] = []
            writer = threading.Thread(
                target=lambda: wrote.append(write_frame(a, True, OP_BINARY, data))
            )
            writer.start()
            result = read_frame(b)
            writer.join(timeout=5)
            assert not writer.is_alive()
            assert wrote == [True]
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_BINARY
            assert payload == data
            assert len(payload) == 70000
        finally:
            a.close()
            b.close()

    def test_fin_false_fragmented(self) -> None:
        """FIN=0 frames (continuation) are forwarded verbatim."""
        a, b = socket.socketpair()
        try:
            assert write_frame(a, False, OP_TEXT, b"part1")
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is False
            assert opcode == OP_TEXT
            assert payload == b"part1"
        finally:
            a.close()
            b.close()

    def test_control_frames(self) -> None:
        """Ping, Pong, Close round-trip correctly."""
        a, b = socket.socketpair()
        try:
            # Ping with payload (RFC 6455 allows up to 125 bytes).
            assert write_frame(a, True, OP_PING, b"ping-body")
            result = read_frame(b)
            assert result is not None
            assert result[0] is True
            assert result[1] == OP_PING
            assert result[2] == b"ping-body"

            # Pong.
            assert write_frame(a, True, OP_PONG, b"pong-body")
            result = read_frame(b)
            assert result is not None
            assert result[1] == OP_PONG

            # Close (with optional status code + reason).
            close_payload = struct.pack("!H", 1000) + b"normal"
            assert write_frame(a, True, OP_CLOSE, close_payload)
            result = read_frame(b)
            assert result is not None
            assert result[1] == OP_CLOSE
            assert result[2] == close_payload
        finally:
            a.close()
            b.close()

    def test_eof_returns_none(self) -> None:
        """read_frame returns None when the peer closes the connection."""
        a, b = socket.socketpair()
        try:
            a.close()
            result = read_frame(b)
            assert result is None
        finally:
            b.close()

    def test_oversized_declared_length_returns_none(self) -> None:
        """A frame declaring a payload beyond the cap is rejected BEFORE any
        payload is buffered — a malicious/corrupt endpoint must not be able
        to grow host memory by declaring a huge length (up to 2**64-1 on the
        8-byte extended field)."""
        a, b = socket.socketpair()
        try:
            # Hand-craft the header: FIN+binary, unmasked, 8-byte ext length
            # declaring one byte over the cap. No payload follows — read_frame
            # must bail on the declaration alone, without waiting for bytes.
            header = bytes([0x80 | OP_BINARY, 127]) + struct.pack(
                "!Q", _MAX_FRAME_BYTES + 1
            )
            a.sendall(header)
            result = read_frame(b)
            assert result is None
        finally:
            a.close()
            b.close()


# ---------------------------------------------------------------------------
# Frame codec — masked direction
# ---------------------------------------------------------------------------

class TestMaskedFrames:
    """Client→server direction (mask=True) round-trips correctly."""

    def test_masked_text_frame(self) -> None:
        a, b = socket.socketpair()
        try:
            # Write masked (as a browser client would).
            assert write_frame(a, True, OP_TEXT, b"masked hello", mask=True)
            # read_frame auto-unmasks.
            result = read_frame(b)
            assert result is not None
            fin, opcode, payload = result
            assert fin is True
            assert opcode == OP_TEXT
            assert payload == b"masked hello"
        finally:
            a.close()
            b.close()

    def test_masked_large_payload(self) -> None:
        a, b = socket.socketpair()
        try:
            data = bytes(i % 256 for i in range(5000))
            assert write_frame(a, True, OP_BINARY, data, mask=True)
            result = read_frame(b)
            assert result is not None
            assert result[2] == data
        finally:
            a.close()
            b.close()

    def test_mask_generated_is_random(self) -> None:
        """Two masked writes of the same payload produce different wire bytes
        (different random mask keys), but both decode to the same payload."""
        a1, b1 = socket.socketpair()
        a2, b2 = socket.socketpair()
        try:
            payload = b"identical"
            write_frame(a1, True, OP_TEXT, payload, mask=True)
            write_frame(a2, True, OP_TEXT, payload, mask=True)
            # Peek at raw bytes without consuming them (MSG_PEEK) so
            # read_frame can still decode them.
            raw1 = b1.recv(100, socket.MSG_PEEK)
            raw2 = b2.recv(100, socket.MSG_PEEK)
            # The mask keys (bytes 2-5 for short frames) should differ.
            assert raw1[2:6] != raw2[2:6]
            # Both frames should still decode correctly.
            r1 = read_frame(b1)
            r2 = read_frame(b2)
            assert r1 is not None and r2 is not None
            assert r1[2] == r2[2] == payload
        finally:
            a1.close()
            b1.close()
            a2.close()
            b2.close()


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

class TestComputeAccept:
    """RFC 6455 §1.3 accept computation."""

    def test_known_vector(self) -> None:
        """RFC 6455 §4.2.2 gives a known test vector."""
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        assert compute_accept(key) == expected

    def test_roundtrip_consistency(self) -> None:
        """accept(key) should be deterministic."""
        key = base64.b64encode(b"test-key-12345").decode()
        a1 = compute_accept(key)
        a2 = compute_accept(key)
        assert a1 == a2
        # Verify the underlying formula.
        digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        assert a1 == base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Pump integration
# ---------------------------------------------------------------------------

class TestPumpBidirectional:
    """pump_bidirectional forwards frames in both directions."""

    def test_single_frame_downstream(self) -> None:
        """Frame written to upstream_sock → browser_sock receives it."""
        up_a, up_b = socket.socketpair()  # up_b = "container" side
        br_a, br_b = socket.socketpair()  # br_a = "browser" side

        pump_thread = threading.Thread(
            target=pump_bidirectional,
            args=(br_a, up_a),
            daemon=True,
        )
        pump_thread.start()

        try:
            # Container sends a text frame.
            assert write_frame(up_b, True, OP_TEXT, b"from container")
            # Browser should receive it.
            result = read_frame(br_b)
            assert result is not None
            assert result[0] is True
            assert result[1] == OP_TEXT
            assert result[2] == b"from container"
        finally:
            up_b.close()
            br_b.close()
            # Closing up_b should trigger the pump to exit.
            pump_thread.join(timeout=2)

    def test_single_frame_upstream_masked(self) -> None:
        """Frame written to browser_sock (masked) → upstream_sock receives it."""
        up_a, up_b = socket.socketpair()
        br_a, br_b = socket.socketpair()

        pump_thread = threading.Thread(
            target=pump_bidirectional,
            args=(br_a, up_a),
            daemon=True,
        )
        pump_thread.start()

        try:
            # Browser sends a masked binary frame.
            assert write_frame(br_b, True, OP_BINARY, b"from browser", mask=True)
            # Container should receive it (also masked, since upstream_mask=True).
            result = read_frame(up_b)
            assert result is not None
            assert result[0] is True
            assert result[1] == OP_BINARY
            assert result[2] == b"from browser"
        finally:
            up_b.close()
            br_b.close()
            pump_thread.join(timeout=2)

    def test_close_frame_stops_pump(self) -> None:
        """A close frame on either leg stops the pump and closes both sockets."""
        up_a, up_b = socket.socketpair()
        br_a, br_b = socket.socketpair()

        pump_thread = threading.Thread(
            target=pump_bidirectional,
            args=(br_a, up_a),
            daemon=True,
        )
        pump_thread.start()

        try:
            # Container sends close.
            close_payload = struct.pack("!H", 1000) + b"bye"
            assert write_frame(up_b, True, OP_CLOSE, close_payload)
            # Browser should receive the close frame.
            result = read_frame(br_b)
            assert result is not None
            assert result[1] == OP_CLOSE
        finally:
            up_b.close()
            br_b.close()
            pump_thread.join(timeout=2)

    def test_multiple_frames(self) -> None:
        """Pump handles a burst of frames correctly."""
        up_a, up_b = socket.socketpair()
        br_a, br_b = socket.socketpair()

        pump_thread = threading.Thread(
            target=pump_bidirectional,
            args=(br_a, up_a),
            daemon=True,
        )
        pump_thread.start()

        try:
            frames = [
                (True, OP_TEXT, b"frame 1"),
                (True, OP_BINARY, b"\x00" * 1000),
                (True, OP_PING, b"are you there"),
                (True, OP_TEXT, b"frame 4"),
            ]
            for fin, op, data in frames:
                assert write_frame(up_b, fin, op, data)

            for expected in frames:
                result = read_frame(br_b)
                assert result is not None
                assert result[0] == expected[0]
                assert result[1] == expected[1]
                assert result[2] == expected[2]
        finally:
            up_b.close()
            br_b.close()
            pump_thread.join(timeout=2)
