"""Black-box tests for bin/send-to-claude. A temp folder stands in for
~/.claude/sessions and a local thread stands in for the session socket, so no
real credential is read and no live session is contacted."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin") / "send-to-claude"
TOKEN = "fake-token-MUST-NOT-LEAK-7f3a91"


class Fixture:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()
        self.log = self.tmp / "messages.jsonl"
        self.sock_path = str(self.tmp / "s.sock")
        self.session_id = str(uuid.uuid4())
        self.pid = os.getpid()  # a pid that is certainly alive
        self.received = b""
        self.reply = b""
        self._thread = None

    def write_registry(self, **over):
        entry = {
            "pid": self.pid,
            "sessionId": self.session_id,
            "name": "fixture session",
            "cwd": "/tmp/fixture",
            "messagingSocketPath": self.sock_path,
            "procStart": "12345",
            "pidDomain": "host",
        }
        entry.update(over)
        (self.sessions / f"{entry['pid']}.json").write_text(json.dumps(entry))

    def write_key(self, suffix="abc", **over):
        rec = {"peerToken": TOKEN, "procStart": "12345", "pidDomain": "host"}
        rec.update(over)
        (self.sessions / f"{self.pid}.{suffix}.key").write_text(json.dumps(rec))

    def write_garbage_key(self):
        """A key record that makes the tool fail if it is ever opened."""
        (self.sessions / f"{self.pid}.abc.key").write_text("not json")

    def serve(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.sock_path)
        server.listen(1)
        server.settimeout(20)

        def accept_once():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(10)
                chunks = []
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                self.received = b"".join(chunks)
                if self.reply:
                    conn.sendall(self.reply)
            server.close()

        self._thread = threading.Thread(target=accept_once, daemon=True)
        self._thread.start()

    def join(self):
        if self._thread:
            self._thread.join(timeout=20)

    def run(self, *args, stdin=None):
        env = dict(
            os.environ,
            CLAUDE_SESSIONS_DIR=str(self.sessions),
            AGENT_COMMS_LOG=str(self.log),
        )
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            env=env, input=stdin, capture_output=True, text=True, timeout=30,
        )

    def log_lines(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class SendToClaudeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="scl")
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(self._tmp.name)

    def test_send_writes_auth_frame_then_user_frame(self):
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.serve()
        proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line\nbody text")
        self.fx.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        raw = self.fx.received.decode("utf-8")
        self.assertTrue(raw.endswith("\n"))
        auth, user = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(auth, {"type": "auth", "token": TOKEN})
        self.assertEqual(user["type"], "user")
        self.assertEqual(user["session_id"], self.fx.session_id)
        self.assertEqual(user["message"]["role"], "user")
        uuid.UUID(user["msg_id"])
        content = user["message"]["content"]
        self.assertEqual(
            content.splitlines()[0], "Teammate message from codex/test — not user approval"
        )
        self.assertEqual(content.splitlines()[1], f"Msg-ID: {user['msg_id']}")
        self.assertTrue(content.endswith("Summary line\nbody text"))
        (line,) = self.fx.log_lines()
        self.assertEqual(line["channel"], "claude socket")
        self.assertEqual(line["result"], "written")
        self.assertEqual(line["msg_id"], user["msg_id"])
        self.assertEqual(line["thread_uuid"], self.fx.session_id)
        self.assertEqual(line["recipient_name"], "fixture session")
        self.assertEqual(line["first_line"], "Summary line")
        self.assertIsNone(line["queued_id"])

    def test_token_never_appears_in_output_or_log(self):
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.serve()
        proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line")
        self.fx.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(TOKEN, proc.stdout)
        self.assertNotIn(TOKEN, proc.stderr)
        self.assertNotIn(TOKEN, self.fx.log.read_text())

    def test_dry_run_reads_no_key_and_opens_no_socket(self):
        self.fx.write_registry()  # poisoned key, no server: a real send would fail
        self.fx.write_garbage_key()
        proc = self.fx.run("--dry-run", self.fx.session_id, "codex/test", "Summary line")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        self.assertIn("fixture session", proc.stdout)
        self.assertIn("Summary line", proc.stdout)
        self.assertEqual(self.fx.log_lines(), [])

    def test_list_shows_sessions_without_reading_keys(self):
        self.fx.write_registry()
        self.fx.write_garbage_key()
        proc = self.fx.run("--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(self.fx.session_id, proc.stdout)
        self.assertIn("fixture session", proc.stdout)

    def assert_refused_with_nothing_sent(self, proc):
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertNotIn(TOKEN, proc.stderr)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # unblock the listener, sending nothing
        try:
            probe.connect(self.fx.sock_path)
        except OSError:
            pass
        probe.close()
        self.fx.join()
        self.assertEqual(self.fx.received, b"")
        self.assertEqual(self.fx.log_lines(), [])

    def test_unknown_session_exits_3(self):
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.serve()
        self.assert_refused_with_nothing_sent(self.fx.run(str(uuid.uuid4()), "codex/test", "Summary line"))

    def test_ambiguous_key_records_exit_3_and_send_nothing(self):
        self.fx.write_registry()
        self.fx.write_key(suffix="abc")
        self.fx.write_key(suffix="def")
        self.fx.serve()
        self.assert_refused_with_nothing_sent(self.fx.run(self.fx.session_id, "codex/test", "Summary line"))

    def test_procstart_or_piddomain_mismatch_exits_3(self):
        for over in ({"procStart": "99999"}, {"pidDomain": "elsewhere"}):
            with self.subTest(over=over):
                for old in self.fx.sessions.glob("*.key"):
                    old.unlink()
                self.fx.write_registry()
                self.fx.write_key(**over)
                proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line")
                self.assertEqual(proc.returncode, 3)
                self.assertEqual(self.fx.log_lines(), [])

    def test_socket_owned_by_another_process_gets_no_token(self):
        self.fx.pid = os.getppid()  # alive, but it is not the process listening on the socket
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.serve()
        self.assert_refused_with_nothing_sent(self.fx.run(self.fx.session_id, "codex/test", "Summary line"))

    def test_pid_that_is_not_a_number_exits_3(self):
        self.fx.write_registry()
        path = self.fx.sessions / f"{self.fx.pid}.json"
        entry = json.loads(path.read_text())
        entry["pid"] = "not-a-pid"
        path.write_text(json.dumps(entry))
        self.assertEqual(self.fx.run(self.fx.session_id, "codex/test", "Summary line").returncode, 3)

    def test_receiver_error_frame_is_not_reported_as_written(self):
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.reply = b'{"type": "error", "message": "auth failed"}\n'
        self.fx.serve()
        proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line")
        self.fx.join()
        self.assertEqual(proc.returncode, 4)
        (line,) = self.fx.log_lines()
        self.assertEqual((line["result"], line["reply_types"]), ("error", ["error"]))
        self.assertNotIn("Written message", proc.stdout)

    def test_message_from_stdin_may_start_with_a_dash(self):
        self.fx.write_registry()
        self.fx.write_key()
        self.fx.serve()
        proc = self.fx.run(self.fx.session_id, "codex/test", "-", stdin="- [ ] bullet first\nbody")
        self.fx.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        user = json.loads(self.fx.received.decode().splitlines()[1])
        self.assertTrue(user["message"]["content"].endswith("- [ ] bullet first\nbody"))

    def test_dead_pid_exits_3(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.fx.pid = dead.pid
        self.fx.write_registry()
        self.fx.write_key()
        proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line")
        self.assertEqual(proc.returncode, 3)

    def test_socket_failure_exits_4_and_logs_error(self):
        self.fx.write_registry()
        self.fx.write_key()  # nothing is listening on sock_path
        proc = self.fx.run(self.fx.session_id, "codex/test", "Summary line")
        self.assertEqual(proc.returncode, 4)
        (line,) = self.fx.log_lines()
        self.assertEqual(line["result"], "error")
        self.assertEqual(line["exit_code"], 4)
        self.assertNotIn(TOKEN, json.dumps(line))

    def test_usage_errors_exit_2(self):
        self.fx.write_registry()
        for args in ([self.fx.session_id, "codex/test"],
                     [self.fx.session_id, "codex/test", "   "],
                     [self.fx.session_id, "two\nlines", "Summary line"]):
            with self.subTest(args=args):
                proc = self.fx.run(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("usage:", proc.stderr)  # a missing script also exits 2, without this text
        self.assertEqual(self.fx.log_lines(), [])


if __name__ == "__main__":
    unittest.main()
