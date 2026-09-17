"""Tests for lib/codex_ws.py against a scripted WebSocket server on a Unix socket."""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.environ.get("AGENT_COMMS_LIB") or ROOT / "lib")))
from codex_ws import CodexError, CodexWS  # noqa: E402

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(opcode, data, fin=True):
    b0 = (0x80 if fin else 0) | opcode
    n = len(data)
    if n < 126:
        head = bytes([b0, n])
    elif n < 65536:
        head = bytes([b0, 126]) + struct.pack(">H", n)
    else:
        head = bytes([b0, 127]) + struct.pack(">Q", n)
    return head + data


def take(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def read(conn):
    b0, b1 = take(conn, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", take(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", take(conn, 8))[0]
    mask = take(conn, 4)
    return b0 & 0x0F, bytes(x ^ mask[i % 4] for i, x in enumerate(take(conn, n)))


def handshake_and_init(conn):
    head = b""
    while b"\r\n\r\n" not in head:
        head += conn.recv(4096)
    key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: " + base64.b64encode(hashlib.sha1(key + GUID).digest()) + b"\r\n\r\n")
    _, data = read(conn)  # initialize
    conn.sendall(frame(1, json.dumps({"id": json.loads(data)["id"], "result": {}}).encode()))
    read(conn)  # initialized


class CodexWSTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "d.sock")
        self.seen = {}

    def serve(self, script):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(1)

        def run():
            conn, _ = server.accept()
            try:
                handshake_and_init(conn)
                script(conn)
            except (ConnectionError, OSError):
                pass
            finally:
                conn.close()
                server.close()

        threading.Thread(target=run, daemon=True).start()

    def test_large_fragmented_answer_with_pings_and_server_requests(self):
        def script(conn):
            _, data = read(conn)
            rid = json.loads(data)["id"]
            self.seen["request_bytes"] = len(data)
            conn.sendall(frame(9, b"pp"))
            conn.sendall(frame(1, json.dumps({"id": rid, "method": "approval/request", "params": {}}).encode()))
            conn.sendall(frame(1, json.dumps({"method": "note"}).encode()))
            out = json.dumps({"id": rid, "result": {"data": "x" * 70000}}).encode()
            conn.sendall(frame(1, out[:100], fin=False))
            conn.sendall(frame(9, b"mid"))
            conn.sendall(frame(0, out[100:300], fin=False))
            conn.sendall(frame(0, out[300:]))
            self.seen["pongs"] = [read(conn), read(conn)]

        self.serve(script)
        ws = CodexWS(self.path)
        self.addCleanup(ws.close)
        result = ws.rpc("thread/list", {"pad": "y" * 70000})  # a request frame above 64 KB
        self.assertEqual(len(result["data"]), 70000)
        time.sleep(0.2)
        self.assertGreater(self.seen["request_bytes"], 70000)
        self.assertEqual(self.seen["pongs"], [(0xA, b"pp"), (0xA, b"mid")])

    def test_medium_frame_uses_the_16_bit_length(self):
        def script(conn):
            _, data = read(conn)
            conn.sendall(frame(1, json.dumps({"id": json.loads(data)["id"], "result": {"data": "z" * 1000}}).encode()))

        self.serve(script)
        self.assertEqual(len(CodexWS(self.path).rpc("x", {})["data"]), 1000)

    def test_bad_replies_become_codex_errors(self):
        for name, payload in (("not json", b"not json"), ("error", None)):
            with self.subTest(name=name):
                if os.path.exists(self.path):
                    os.unlink(self.path)

                def script(conn, payload=payload):
                    _, data = read(conn)
                    rid = json.loads(data)["id"]
                    conn.sendall(frame(1, payload or json.dumps({"id": rid, "error": {"message": "no"}}).encode()))

                self.serve(script)
                with self.assertRaises(CodexError):
                    CodexWS(self.path).rpc("x", {})

    def test_non_dict_frames_are_ignored_and_null_result_is_empty(self):
        def script(conn):
            _, data = read(conn)
            conn.sendall(frame(1, b"[1]"))
            conn.sendall(frame(1, json.dumps({"id": json.loads(data)["id"], "result": None}).encode()))

        self.serve(script)
        self.assertEqual(CodexWS(self.path).rpc("x", {}), {})

    def test_a_chatty_daemon_cannot_stall_a_call_forever(self):
        def script(conn):
            read(conn)
            for _ in range(40):
                conn.sendall(frame(1, b'{"method": "tick"}'))
                time.sleep(0.25)

        self.serve(script)
        ws = CodexWS(self.path, timeout=1)
        started = time.monotonic()
        with self.assertRaises(CodexError):
            ws.rpc("x", {})
        self.assertLess(time.monotonic() - started, 3)

    def test_no_daemon_is_a_codex_error(self):
        with self.assertRaises(CodexError):
            CodexWS(self.path)


if __name__ == "__main__":
    unittest.main()
