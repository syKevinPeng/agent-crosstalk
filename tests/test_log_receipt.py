"""Black-box tests for bin/log-receipt."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin") / "log-receipt"
MSG_ID = "11111111-2222-4333-8444-555555555555"


class LogReceiptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lr")
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "messages.jsonl"
        self.log.write_text(json.dumps({"channel": "claude socket", "msg_id": MSG_ID, "result": "written"}) + "\n")

    def run_tool(self, *args):
        env = dict(os.environ, AGENT_COMMS_LOG=str(self.log))
        return subprocess.run([str(TOOL), *args], env=env, capture_output=True, text=True, timeout=30)

    def lines(self):
        out = []
        for line in self.log.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def test_appends_a_receipt_for_a_known_msg_id(self):
        proc = self.run_tool(MSG_ID, "codex/reviewer", "reply REVIEW-006 cites the Msg-ID")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        original, receipt = self.lines()
        self.assertEqual(original["result"], "written")  # the send line is never rewritten
        self.assertEqual(receipt["channel"], "receipt")
        self.assertEqual(receipt["msg_id"], MSG_ID)
        self.assertEqual(receipt["confirmed_by"], "codex/reviewer")
        self.assertIn("REVIEW-006", receipt["evidence"])
        self.assertRegex(receipt["time_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_unknown_msg_id_exits_3_and_appends_nothing(self):
        proc = self.run_tool("99999999-2222-4333-8444-555555555555", "codex/reviewer", "evidence")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(len(self.lines()), 1)

    def test_a_torn_line_does_not_hide_the_message(self):
        with open(self.log, "a") as fh:
            fh.write('{"truncated": \n"just a string"\n')
        proc = self.run_tool(MSG_ID, "codex/reviewer", "evidence")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.log.read_text().splitlines()[-1].count('"receipt"'), 1)

    def test_a_failed_send_gets_no_receipt(self):
        failed = "22222222-2222-4333-8444-555555555555"
        with open(self.log, "a") as fh:
            fh.write(json.dumps({"channel": "claude socket", "msg_id": failed, "result": "error"}) + "\n")
        self.assertEqual(self.run_tool(failed, "codex/reviewer", "evidence").returncode, 3)

    def test_an_id_that_exists_only_as_a_receipt_is_refused(self):
        ghost = "33333333-2222-4333-8444-555555555555"
        with open(self.log, "a") as fh:
            fh.write(json.dumps({"channel": "receipt", "msg_id": ghost}) + "\n")
        self.assertEqual(self.run_tool(ghost, "codex/reviewer", "evidence").returncode, 3)

    def test_usage_errors_exit_2(self):
        for args in ([MSG_ID, "codex/reviewer"], [MSG_ID, "", "evidence"], [MSG_ID, "codex/reviewer", "  "]):
            with self.subTest(args=args):
                self.assertEqual(self.run_tool(*args).returncode, 2)
        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
