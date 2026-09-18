"""Client for the local Codex app-server daemon: JSON requests in WebSocket text
frames over a Unix socket. Standard library only. Checked against codex-cli
0.154.0, whose app-server protocol is marked experimental: recheck after upgrades."""
import base64
import json
import os
import socket
import struct
import time

DEFAULT_SOCK = "~/.codex/app-server-control/app-server-control.sock"


class CodexError(Exception):
    """The daemon could not be reached or it refused a request."""


def sock_path():
    return os.environ.get("CODEX_APP_SERVER_SOCK") or os.path.expanduser(DEFAULT_SOCK)


class CodexWS:
    def __init__(self, path=None, timeout=20, experimental=False):
        """`experimental` opts into methods Codex marks experimental, such as `thread/queue/list`."""
        self._next_id = 0
        self._timeout = timeout
        self._buf = b""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(path or sock_path())
            key = base64.b64encode(os.urandom(16)).decode()
            self._sock.sendall(
                ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
            )
            while b"\r\n\r\n" not in self._buf:
                self._fill()
        except OSError as exc:
            raise CodexError(f"cannot reach the Codex daemon: {exc.strerror or exc}") from None
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0]
        if b" 101 " not in status + b" ":
            raise CodexError("daemon refused the WebSocket upgrade: " + status.decode(errors="replace")[:80])
        params = {"clientInfo": {"name": "agent-comms", "version": "1"}}
        if experimental:
            params["capabilities"] = {"experimentalApi": True}
        self.rpc("initialize", params)
        self._send({"method": "initialized"})

    def _fill(self):
        data = self._sock.recv(65536)
        if not data:
            raise CodexError("daemon closed the connection")
        self._buf += data

    def _take(self, n):
        while len(self._buf) < n:
            self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        self._sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _send(self, obj):
        self._frame(0x1, json.dumps(obj).encode("utf-8"))

    def _recv(self):
        parts = []
        while True:
            b0, b1 = self._take(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            data = self._take(n)
            if opcode == 0x8:
                raise CodexError("daemon closed the connection")
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                try:
                    return json.loads(b"".join(parts))
                except ValueError:
                    raise CodexError("daemon sent a frame that is not JSON") from None

    def rpc(self, method, params):
        """Send one request and return its `result`. Notifications in between are skipped."""
        self._next_id += 1
        rid = self._next_id
        deadline = time.monotonic() + self._timeout
        try:
            self._send({"id": rid, "method": method, "params": params})
            while True:
                if time.monotonic() > deadline:
                    raise CodexError(f"{method}: no answer within {self._timeout} s")
                reply = self._recv()
                # An answer is a dict with our id and no "method". Anything else is a
                # notification or a server-side request, which this client ignores.
                if isinstance(reply, dict) and reply.get("id") == rid and "method" not in reply:
                    break
        except OSError as exc:
            raise CodexError(f"{method}: {exc.strerror or exc}") from None
        if "error" in reply:
            raise CodexError(f"{method}: {json.dumps(reply['error'])[:300]}")
        return reply.get("result") or {}

    def close(self):
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        self._sock.close()
