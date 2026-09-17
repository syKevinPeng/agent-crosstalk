"""Tests for lib/menu_actions.py against the stub tmux. They assert the exact keys sent,
and that nothing is sent when a safety rule says no."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from menu_fixtures import LIB, Machine

sys.path.insert(0, str(LIB))
import agent_state  # noqa: E402
import menu_actions  # noqa: E402


class MenuActionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m.pane(7, 700, "codex", "builder", screen="› Ask Codex to do anything\n")
        self.agent = agent_state.Agent(key="codex:t1", kind="codex", name="builder", session_id="t1",
                                       pane_id="%7", pane_pid=700)

    def sent(self):
        return [c for c in self.m.tmux_calls() if c.startswith("send-keys")]

    def test_instruction_is_typed_literally_then_enter_and_logged(self):
        self.assertEqual(menu_actions.instruct(self.agent, "  Enter the review now  "), "typed into its pane")
        self.assertEqual(self.sent(), ["send-keys -t %7 -l -- Enter the review now", "send-keys -t %7 Enter"])
        (line,) = self.m.actions()
        self.assertEqual((line["action"], line["result"], line["instruction"], line["agent_name"]),
                         ("instruct", "typed into pane", "Enter the review now", "builder"))

    def test_an_auth_prompt_blocks_typing_and_logs_only_the_label(self):
        (self.m.dir / "screen-7.txt").write_text("Cloning...\nPassword: ")
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(self.agent, "hunter2")
        self.assertEqual(self.sent(), [])
        (line,) = self.m.actions()
        self.assertEqual((line["action"], line["request_text"]), ("refused", "password"))
        self.assertNotIn("hunter2", (self.m.dir / "menu-actions.jsonl").read_text())

    def test_a_pane_that_now_runs_something_else_gets_no_keys(self):
        (self.m.dir / "pid-7.txt").write_text("999\n")
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(self.agent, "hello")
        (self.m.dir / "pid-7.txt").unlink()            # the pane is gone altogether
        with self.assertRaises(menu_actions.Refused):
            menu_actions.open_pane(self.agent)
        self.assertEqual(self.sent(), [])
        self.assertFalse(any(c.startswith("select-") for c in self.m.tmux_calls()))

    def test_an_unwritable_log_refuses_the_action(self):
        blocked = Path(self._tmp.name) / "a-folder"
        blocked.mkdir()
        with mock.patch.dict(os.environ, {"AGENT_MENU_ACTIONS_LOG": str(blocked)}):
            for action in (lambda: menu_actions.instruct(self.agent, "hello"),
                           lambda: menu_actions.open_pane(self.agent)):
                with self.assertRaises(menu_actions.Refused):
                    action()
        self.assertEqual([c for c in self.m.tmux_calls() if not c.startswith(("list-", "capture-", "display-"))], [])

    def test_a_headless_codex_gets_plain_owner_input_with_no_teammate_header(self):
        argv_file = self.m.dir / "codex-argv.txt"
        stub = self.m.dir / "codex-stub"
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {argv_file}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        headless = agent_state.Agent(key="codex:t2", kind="codex", name="quiet", session_id="t2")
        with mock.patch.dict(os.environ, {"CODEX_BIN": str(stub)}):
            self.assertEqual(menu_actions.instruct(headless, "Please retry the push"), "queued for it")
        self.assertEqual(argv_file.read_text().splitlines(), ["queue", "--thread", "t2", "--message", "Please retry the push"])
        self.assertNotIn("Teammate message", argv_file.read_text())

    def test_a_claude_session_with_no_pane_takes_no_instruction(self):
        far = agent_state.Agent(key="claude:s", kind="claude", name="far", session_id="s", short_id="ssssssss")
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(far, "hello")
        self.assertEqual(menu_actions.attach(far), "opened in a new window")
        self.assertTrue(any(c.startswith("new-window -n far ") and c.endswith("attach ssssssss")
                            for c in self.m.tmux_calls()))

    def test_only_a_spawned_agent_can_be_retired(self):
        with self.assertRaises(menu_actions.Refused):
            menu_actions.retire(self.agent)
        self.assertEqual(self.m.actions(), [])

    def test_open_pane_focuses_and_logs(self):
        self.assertEqual(menu_actions.open_pane(self.agent), "pane focused")
        self.assertIn("select-pane -t %7", self.m.tmux_calls())
        self.assertEqual(self.m.actions()[0]["action"], "open_pane")


if __name__ == "__main__":
    unittest.main()
