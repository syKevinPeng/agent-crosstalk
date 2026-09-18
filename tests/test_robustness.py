"""Robustness of the messaging and peer tools, from the product review: long and odd input, interrupts,
torn logs, changed daemon answers, and exit codes that mean one thing each. Stubs and fakes only."""
import gc
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from menu_fixtures import LIB  # noqa: E402
from test_peers import BIN, CLAUDE_STUB, QUEUEING_STUB, FakeCodexDaemon  # noqa: E402
from test_send_to_claude import Fixture as ClaudeSocketFixture  # noqa: E402

sys.path.insert(0, str(LIB))
from codex_ws import CodexError, CodexWS  # noqa: E402

THREAD = "0a0a0a0a-0000-7000-8000-000000000001"
MSG = "11111111-2222-4333-8444-555555555555"


class SendToCodexInput(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rb")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.daemon = FakeCodexDaemon(str(self.tmp / "d.sock"))
        self.daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": "/w"}
        self.daemon.status[THREAD] = "idle"
        self.stub = self.tmp / "codex-stub"
        self.stub.write_text(QUEUEING_STUB)
        self.stub.chmod(0o755)
        self.log = self.tmp / "m.jsonl"

    def env(self, **extra):
        env = dict(os.environ, CODEX_BIN=str(self.stub), STUB_DIR=str(self.tmp),
                   CODEX_APP_SERVER_SOCK=str(self.tmp / "d.sock"), AGENT_COMMS_LOG=str(self.log),
                   AGENT_COMMS_SPAWN_LOG=str(self.tmp / "s.jsonl"), SEND_TO_CODEX_WAIT_SECONDS="1")
        env.update(extra)
        return env

    def send(self, message, **extra):
        return subprocess.run([str(BIN / "send-to-codex"), THREAD, "claude/t", "-"], input=message, env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)

    def lines(self):
        """The log's JSON lines. A torn line is not JSON and is left out, as every reader here does."""
        out = []
        for line in self.log.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def test_a_long_message_on_stdin_is_sent(self):
        # A short first line and a long body: awk stopped reading early and SIGPIPE killed the tool.
        proc = self.send("Summary line\n" + "a" * 100_000)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines()[0]["first_line"], "Summary line")

    def test_a_message_too_long_for_one_argument_is_refused_before_sending(self):
        # `codex queue` takes the message as one argument, and Linux caps one argument at 128 KiB.
        proc = self.send("Summary\n" + "a" * 140_000)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("too long", proc.stderr)
        self.assertNotIn("thread/queue/add", self.daemon.methods())
        self.assertFalse(self.log.exists() and self.log.read_text())

    def test_a_bad_wait_is_a_usage_error_before_sending(self):
        for wait in ("nan", "abc", "-1", "100000"):
            with self.subTest(wait=wait):
                proc = self.send("Summary", SEND_TO_CODEX_WAIT_SECONDS=wait)
                self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("thread/queue/add", self.daemon.methods())

    def test_an_interrupt_during_the_wait_still_logs_the_send(self):
        self.daemon.stall = True                                 # queued, and no turn takes it
        proc = subprocess.Popen([str(BIN / "send-to-codex"), THREAD, "claude/t", "Summary"],
                                env=self.env(SEND_TO_CODEX_WAIT_SECONDS="30"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while not self.daemon.queue.get(THREAD) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)                                          # inside the delivery wait
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130)
        (line,) = self.lines()
        self.assertEqual((line["result"], line["delivery"]), ("queued", "interrupted"))
        self.assertTrue(line["msg_id"])

    def test_a_torn_last_line_does_not_swallow_the_next(self):
        self.log.write_text('{"channel": "codex queue", "msg_id": "torn')      # a writer died mid-line
        self.assertEqual(self.send("Summary").returncode, 0)
        (line,) = self.lines()                                   # the new line stands on its own
        self.assertEqual(line["first_line"], "Summary")

    def test_codex_failure_is_exit_4_and_the_log_keeps_its_own_status(self):
        stub = self.tmp / "failing"
        stub.write_text("#!/usr/bin/env bash\necho 'error: unexpected argument' >&2\nexit 2\n")
        stub.chmod(0o755)
        proc = self.send("Summary", CODEX_BIN=str(stub))
        self.assertEqual(proc.returncode, 4)                     # never 2, which means a usage error here
        self.assertEqual((self.lines()[0]["result"], self.lines()[0]["exit_code"]), ("error", 2))

    def test_queued_but_not_taken_is_exit_6_as_in_codex_reply(self):
        self.daemon.stall = True
        self.assertEqual(self.send("Summary").returncode, 6)


class LogReceiptInput(unittest.TestCase):
    def test_a_torn_last_line_does_not_swallow_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "m.jsonl"
            log.write_text(json.dumps({"channel": "codex queue", "msg_id": MSG, "result": "queued"}) + "\n" + '{"torn')
            proc = subprocess.run([str(BIN / "log-receipt"), MSG, "codex/x", "cited it"], capture_output=True,
                                  text=True, timeout=30, env=dict(os.environ, AGENT_COMMS_LOG=str(log)))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            last = json.loads(log.read_text().splitlines()[-1])
            self.assertEqual((last["channel"], last["msg_id"]), ("receipt", MSG))


class SendToClaudeInput(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rc")
        self.addCleanup(self._tmp.cleanup)
        self.fx = ClaudeSocketFixture(self._tmp.name)

    def test_a_name_with_a_lone_surrogate_is_delivered_and_logged(self):
        # A JavaScript writer that cuts through an emoji leaves half a surrogate pair in the registry.
        self.fx.write_registry(name="cut \ud83d")
        self.fx.write_key()
        self.fx.serve()
        proc = self.fx.run(self.fx.session_id, "codex/t", "Hello")
        self.fx.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.fx.log_lines()), 1)
        self.assertEqual(self.fx.run("--list").returncode, 0)

    def test_bad_utf8_on_stdin_is_a_usage_error(self):
        self.fx.write_registry()
        env = dict(os.environ, CLAUDE_SESSIONS_DIR=str(self.fx.sessions), AGENT_COMMS_LOG=str(self.fx.log))
        proc = subprocess.run([sys.executable, str(BIN / "send-to-claude"), self.fx.session_id, "codex/t", "-"],
                              input=b"caf\xc3 broken", env=env, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)


class PeerLifecycleInput(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pl")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        for sub in (".claude", ".codex", "work"):
            (self.home / sub).mkdir(parents=True)
        self.sock = str(self.tmp / "d.sock")
        claude = self.tmp / "claude-stub"
        claude.write_text(CLAUDE_STUB)
        claude.chmod(0o755)
        codex = self.tmp / "codex-stub"
        codex.write_text(QUEUEING_STUB)
        codex.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_APP_SERVER_SOCK=self.sock, CLAUDE_BIN=str(claude),
                        CODEX_BIN=str(codex), STUB_DIR=str(self.tmp), AGENT_COMMS_SPAWN_LOG=str(self.tmp / "spawned.jsonl"),
                        AGENT_COMMS_LOG=str(self.tmp / "m.jsonl"), SPAWN_PEER_POLL_SECONDS="1",
                        SEND_TO_CODEX_WAIT_SECONDS="1")

    def run_tool(self, *args, **extra):
        return subprocess.run([sys.executable, *(os.fsencode(a) if isinstance(a, str) else a for a in args)],
                              env=dict(self.env, **extra), capture_output=True, timeout=120)

    def test_a_name_that_is_not_utf8_is_refused_before_anything_is_created(self):
        daemon = FakeCodexDaemon(self.sock)
        proc = self.run_tool(str(BIN / "spawn-peer"), "--cwd", str(self.home / "work"), "codex", b"peer-\xff", "claude/t")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("thread/start", daemon.methods())

    def test_a_first_message_no_turn_takes_is_exit_5(self):
        daemon = FakeCodexDaemon(self.sock)
        daemon.stall = True
        proc = self.run_tool(str(BIN / "spawn-peer"), "--cwd", str(self.home / "work"), "codex", "peer", "claude/t", "Hello")
        self.assertEqual(proc.returncode, 5, proc.stderr)       # created and recorded; the message waits
        self.assertIn("thread/start", daemon.methods())

    def test_a_changed_daemon_answer_is_a_failure_not_a_traceback(self):
        daemon = FakeCodexDaemon(self.sock)
        daemon.replies["thread/list"] = ["not", "an", "object"]
        proc = self.run_tool(str(BIN / "spawn-peer"), "--cwd", str(self.home / "work"), "codex", "peer", "claude/t")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_retire_calls_an_unreadable_listing_a_failure(self):
        with open(self.tmp / "spawned.jsonl", "w") as fh:
            fh.write(json.dumps({"event": "spawned", "kind": "claude", "name": "kid", "id": "ab12cd34",
                                 "cwd": "/w", "access": "read-only", "spawned_by": "codex/x"}) + "\n")
        (self.tmp / "launched").write_text("")                  # makes the stub's `claude agents` fail
        proc = self.run_tool(str(BIN / "retire-peer"), "ab12cd34", "codex/x", STUB_AGENTS_FAIL_AFTER_LAUNCH="1")
        self.assertEqual(proc.returncode, 4, proc.stderr)


class PythonLogWriter(unittest.TestCase):
    def test_a_torn_last_line_does_not_swallow_the_next(self):
        import private_log
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "spawned.jsonl"
            log.write_text('{"event": "spawn')                  # a writer died mid-line
            private_log.append_line(str(log), json.dumps({"event": "spawned", "id": "x"}))
            self.assertEqual(json.loads(log.read_text().splitlines()[-1]), {"event": "spawned", "id": "x"})


class CodexWSCleanup(unittest.TestCase):
    def test_a_failed_handshake_closes_its_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = FakeCodexDaemon(os.path.join(tmp, "d.sock"))
            daemon.fail.add("initialize")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with self.assertRaises(CodexError):
                    CodexWS(os.path.join(tmp, "d.sock"))
                gc.collect()
            self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])


if __name__ == "__main__":
    unittest.main()
