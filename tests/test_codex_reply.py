"""Black-box tests for bin/codex-reply against the fake daemon from test_peers."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_peers import BIN, THREAD_ID, FakeCodexDaemon  # noqa: E402

TOOL = BIN / "codex-reply"


class CodexReplyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cr")
        self.addCleanup(self._tmp.cleanup)
        self.sock = os.path.join(self._tmp.name, "d.sock")
        self.daemon = FakeCodexDaemon(self.sock)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, CODEX_APP_SERVER_SOCK=self.sock))

    def set_turns(self, turns):
        self.daemon.threads[THREAD_ID] = {"id": THREAD_ID, "name": "comms-test", "cwd": "/w", "turns": turns}

    def test_prints_only_the_final_answer_of_the_latest_turn(self):
        self.set_turns([
            {"status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": "old answer"}]},
            {"status": "completed", "items": [
                {"type": "userMessage", "text": "the request"},
                {"type": "agentMessage", "phase": "commentary", "text": "working on it"},
                {"type": "commandExecution", "command": "ls"},
                {"type": "agentMessage", "phase": "final_answer", "text": "ACK 1234"}]},
        ])
        proc = self.run_tool(THREAD_ID)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ACK 1234", proc.stdout)
        self.assertIn("latest turn: completed", proc.stdout)
        for absent in ("old answer", "working on it", "the request"):
            self.assertNotIn(absent, proc.stdout)
        self.assertIn("working on it", self.run_tool("--all", THREAD_ID).stdout)

    def test_it_only_reads(self):
        self.set_turns([{"status": "completed", "items": [{"type": "agentMessage", "text": "hi"}]}])
        self.run_tool(THREAD_ID)
        self.assertEqual(self.daemon.methods(), ["initialize", "initialized", "thread/read"])

    def test_running_turn_exits_5_and_shows_what_exists(self):
        self.set_turns([{"status": {"type": "inProgress"},
                         "items": [{"type": "agentMessage", "phase": "commentary", "text": "still going"}]}])
        proc = self.run_tool(THREAD_ID)
        self.assertEqual(proc.returncode, 5)
        self.assertIn("still going", proc.stdout)

    def test_no_turn_yet_exits_3(self):
        self.set_turns([])
        self.assertEqual(self.run_tool(THREAD_ID).returncode, 3)

    def test_daemon_refusal_exits_4(self):
        self.daemon.fail.add("thread/read")
        self.assertEqual(self.run_tool(THREAD_ID).returncode, 4)


if __name__ == "__main__":
    unittest.main()
