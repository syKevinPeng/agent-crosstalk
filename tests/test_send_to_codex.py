"""Black-box tests for bin/send-to-codex. A stub stands in for `codex`, so
nothing is queued to a real session."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin") / "send-to-codex"
THREAD = "0b0b0b0b-0000-7000-8000-000000000001"


def make_stub(tmp, exit_code, output):
    """Write a fake codex that records its argv, prints `output`, exits `exit_code`."""
    stub = Path(tmp) / "codex-stub"
    argv_file = Path(tmp) / "argv.txt"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {argv_file}\n"
        f"printf '%s\\n' {json.dumps(output)}\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, argv_file


def run_tool(tmp, stub, *args, stdin=None):
    env = dict(os.environ, CODEX_BIN=str(stub), AGENT_COMMS_LOG=str(Path(tmp) / "messages.jsonl"))
    return subprocess.run(
        [str(TOOL), *args], env=env, input=stdin, capture_output=True, text=True, timeout=30
    )


def read_log(tmp):
    path = Path(tmp) / "messages.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class SendToCodexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stc")
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_success_queues_and_logs_one_line(self):
        stub, argv_file = make_stub(self.tmp, 0, f"Queued message q-123 for thread {THREAD}.")
        proc = run_tool(self.tmp, stub, THREAD, "claude/test", "Summary line\nbody")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = argv_file.read_text()
        self.assertIn("queue", argv)
        self.assertIn("Teammate message from claude/test — not user approval", argv)
        (line,) = read_log(self.tmp)
        self.assertEqual(line["channel"], "codex queue")
        self.assertEqual(line["result"], "queued")
        self.assertEqual(line["queued_id"], "q-123")
        self.assertEqual(line["thread_uuid"], THREAD)
        self.assertEqual(line["first_line"], "Summary line")
        self.assertEqual(line["exit_code"], 0)

    def test_codex_failure_exits_nonzero_and_logs_error(self):
        stub, _ = make_stub(self.tmp, 7, "error: no such thread")
        proc = run_tool(self.tmp, stub, THREAD, "claude/test", "Summary line")
        self.assertEqual(proc.returncode, 7)
        (line,) = read_log(self.tmp)
        self.assertEqual(line["result"], "error")
        self.assertEqual(line["exit_code"], 7)
        self.assertIn("no such thread", line["error"])
        self.assertIsNone(line["queued_id"])

    def test_dry_run_sends_nothing_and_logs_nothing(self):
        stub, argv_file = make_stub(self.tmp, 0, "should not run")
        proc = run_tool(self.tmp, stub, "--dry-run", THREAD, "claude/test", "Summary line")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        self.assertFalse(argv_file.exists())
        self.assertEqual(read_log(self.tmp), [])

    def test_message_from_stdin(self):
        stub, argv_file = make_stub(self.tmp, 0, f"Queued message q-9 for thread {THREAD}.")
        proc = run_tool(self.tmp, stub, THREAD, "claude/test", "-", stdin="From stdin\nmore")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("From stdin", argv_file.read_text())
        self.assertEqual(read_log(self.tmp)[0]["first_line"], "From stdin")

    def test_usage_errors_exit_2(self):
        stub, argv_file = make_stub(self.tmp, 0, "should not run")
        for args in ([THREAD, "claude/test"], [THREAD, "claude/test", "   "], ["-x", "a", "b", "c"]):
            with self.subTest(args=args):
                proc = run_tool(self.tmp, stub, *args)
                self.assertEqual(proc.returncode, 2)
        self.assertFalse(argv_file.exists())
        self.assertEqual(read_log(self.tmp), [])

    def test_msg_id_is_stamped_in_message_and_log(self):
        stub, argv_file = make_stub(self.tmp, 0, f"Queued message q-1 for thread {THREAD}.")
        proc = run_tool(self.tmp, stub, THREAD, "claude/test", "Summary line")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (line,) = read_log(self.tmp)
        self.assertRegex(line["msg_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        message_lines = argv_file.read_text().splitlines()
        header_at = message_lines.index("Teammate message from claude/test — not user approval")
        self.assertEqual(message_lines[header_at + 1], f"Msg-ID: {line['msg_id']}")
        self.assertEqual(message_lines[header_at + 2], "")


if __name__ == "__main__":
    unittest.main()
