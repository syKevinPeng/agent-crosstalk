"""Tests for lib/menu_actions.py against the stub tmux. They assert the exact keys sent,
and that nothing is sent when a safety rule says no."""
import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from menu_fixtures import LIB, FakeCodexDaemon, Machine

sys.path.insert(0, str(LIB))
import agent_state  # noqa: E402
import auth_readers  # noqa: E402
import menu_actions  # noqa: E402
from sources import tmux_src  # noqa: E402

THREAD = "0c0c0c0c-0000-7000-8000-00000000abcd"
ENTER_DELAY = tmux_src.ENTER_DELAY      # the shipped value, read before any test patches it


class MenuActionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ma")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        no_wait = mock.patch.object(tmux_src, "ENTER_DELAY", 0)      # the stub pane needs no settling time
        no_wait.start()
        self.addCleanup(no_wait.stop)
        self.m.pane(7, 700, "codex", "builder | proj", screen="› Ask Codex to do anything\n")
        self.agent = agent_state.Agent(key="codex:t1", kind="codex", name="builder", session_id="t1",
                                       pane_id="%7", pane_pid=700)

    def acted(self):
        """tmux calls that change something, as opposed to reading."""
        return [c for c in self.m.tmux_calls() if not c.startswith(("list-panes", "capture-pane", "display-message"))]

    def test_instruction_is_typed_literally_then_enter_with_attempt_logged_first(self):
        self.assertEqual(menu_actions.instruct(self.agent, "  Enter the review now  "), "typed into its pane")
        self.assertEqual(self.acted(), ["send-keys -t %7 -l -- Enter the review now", "send-keys -t %7 Enter"])
        attempt, result = self.m.actions()
        self.assertEqual((attempt["action"], attempt["result"], attempt["instruction"]),
                         ("instruct", "attempt", "Enter the review now"))
        self.assertEqual((result["action"], result["result"], result["agent_name"]),
                         ("instruct", "typed into pane", "builder"))

    def test_the_attempt_line_must_be_written_or_nothing_happens(self):
        blocked = Path(self._tmp.name) / "a-folder"
        blocked.mkdir()
        far = agent_state.Agent(key="claude:s", kind="claude", name="far", session_id="s",
                                short_id="abcdef12", background=True)
        spawned = agent_state.Agent(key="codex:t9", kind="codex", name="kid", session_id="t9", spawned=True)
        with mock.patch.dict(os.environ, {"AGENT_MENU_ACTIONS_LOG": str(blocked)}):
            for action in (lambda: menu_actions.instruct(self.agent, "hello"),
                           lambda: menu_actions.open_pane(self.agent),
                           lambda: menu_actions.attach(far),
                           lambda: menu_actions.retire(spawned)):
                with self.assertRaises(menu_actions.Refused):
                    action()
        self.assertEqual(self.acted(), [])

    def test_a_failing_result_line_does_not_undo_or_crash_the_action(self):
        real_log, calls = menu_actions._log, []

        def flaky(agent, action, result, **extra):
            calls.append(result)
            if result != "attempt":
                raise OSError(28, "No space left on device")
            return real_log(agent, action, result, **extra)
        with mock.patch.object(menu_actions, "_log", flaky):
            self.assertEqual(menu_actions.instruct(self.agent, "hello"), "typed into its pane")
        self.assertEqual(calls, ["attempt", "typed into pane"])
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt"])     # the record exists

    def test_an_auth_prompt_blocks_typing_and_logs_only_the_label(self):
        (self.m.dir / "screen-7.txt").write_text("Cloning...\n  ⎿  Password for 'https://u@host': \n──\n❯ \n──\n  footer\n")
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(self.agent, "hunter2")
        self.assertEqual(self.acted(), [])
        (line,) = self.m.actions()
        self.assertEqual((line["action"], line["request_text"]), ("refused", "password"))
        log_text = (self.m.dir / "menu-actions.jsonl").read_text()
        self.assertNotIn("hunter2", log_text)
        self.assertNotIn("https://u@host", log_text)

    def test_a_pane_is_refused_when_its_pid_or_its_foreground_program_changed(self):
        self.m.panes = [(7, 999, "codex", "builder | proj")]                  # another process owns the pane
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(self.agent, "hello")
        self.m.panes = [(7, 700, "bash", "builder | proj")]                   # codex exited, its shell kept the pid
        self.m.write()
        for action in (lambda: menu_actions.instruct(self.agent, "rm -rf build"), lambda: menu_actions.open_pane(self.agent)):
            with self.assertRaises(menu_actions.Refused):
                action()
        self.m.panes = []                                                     # the pane is gone
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.open_pane(self.agent)
        self.assertEqual(self.acted(), [])
        self.assertEqual(self.m.actions(), [])

    def test_a_claude_session_restarted_in_the_same_pane_gets_no_keys(self):
        """The CLI exits, the owner starts another session in the same shell: same pid, same name."""
        self.m.pane(4, 400, "claude", "lead")      # this also puts pid 400 in the fake /proc
        self.m.process(500, 400)
        session = agent_state.Agent(key="claude:s", kind="claude", name="lead", session_id="s",
                                    pane_id="%4", pane_pid=400, pid=500)
        self.assertEqual(menu_actions.open_pane(session), "pane focused")
        shutil.rmtree(self.m.proc / "500")         # the old session exited; another one now owns the pane
        self.m.process(501, 400)
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(session, "hello")
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys")], [])

    def test_a_pane_that_now_shows_another_session_gets_no_keys(self):
        self.m.panes = [(7, 700, "codex", "a different thread | proj")]     # same shell, same CLI, another session
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(self.agent, "hello")
        self.assertEqual(self.acted(), [])

    def test_perform_tells_the_owner_instead_of_crashing(self):
        self.assertEqual(menu_actions.perform(self.agent, "Open pane"), ("pane focused", True, True))
        self.assertEqual(menu_actions.perform(self.agent, "Send", "hello")[:2], ("typed into its pane", True))
        note, ok, close = menu_actions.perform(self.agent, "Quit agent")    # in a pane: refused
        self.assertEqual((ok, close), (False, False))
        self.assertTrue(note.startswith("not done:"))
        for error in (OSError(28, "No space left"), subprocess.TimeoutExpired("x", 1)):
            with mock.patch.object(menu_actions, "open_pane", side_effect=error):
                note, ok, close = menu_actions.perform(self.agent, "Open pane")
            self.assertEqual((ok, close), (False, False))
            self.assertIn(type(error).__name__, note)
        self.assertEqual(menu_actions.perform(self.agent, "No such button"), ("", False, False))

    def test_look_only_mode_refuses_send_and_quit_even_if_called(self):
        with mock.patch.dict(os.environ, {"AGENT_MENU_LOOK_ONLY": "1"}):
            for button in ("Send", "Quit agent"):
                self.assertEqual(menu_actions.perform(self.agent, button, "hello"), ("not done: look-only mode", False, False))
            self.assertEqual(menu_actions.perform(self.agent, "Open pane")[1], True)
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys")], [])

    def test_a_forgotten_stub_can_never_reach_the_real_codex(self):
        headless = agent_state.Agent(key="codex:t2", kind="codex", name="quiet", session_id="t2")
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(headless, "hello")                        # the fixture's codex stub fails
        self.assertEqual((self.m.dir / "codex-calls.txt").read_text().strip(), "queue --thread t2 --message hello")
        self.assertIn("codex-stub", os.environ["CODEX_BIN"])

    def test_an_instruction_is_one_line_of_plain_text(self):
        for text in ("first\nsecond", "tab\there", "esc\x1b[2J", "bell\x07"):
            with self.subTest(text=text):
                with self.assertRaises(menu_actions.Refused):
                    menu_actions.instruct(self.agent, text)
        self.assertEqual(self.acted(), [])
        self.assertEqual(menu_actions.instruct(self.agent, "-rf $(id) `id` #{pane_pid} ünï"), "typed into its pane")
        self.assertIn("send-keys -t %7 -l -- -rf $(id) `id` #{pane_pid} ünï", self.acted())

    def test_a_trailing_semicolon_survives(self):
        menu_actions.instruct(self.agent, "run tests; then stop;")
        self.assertIn("send-keys -t %7 -l -- run tests; then stop\;", self.acted())
        self.assertEqual(self.m.actions()[0]["instruction"], "run tests; then stop;")

    def test_a_headless_codex_gets_plain_owner_input_with_no_teammate_header(self):
        argv_file = self.m.dir / "codex-argv.txt"
        stub = self.m.dir / "codex-stub"
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {argv_file}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        headless = agent_state.Agent(key="codex:t2", kind="codex", name="quiet", session_id="t2")
        with mock.patch.dict(os.environ, {"CODEX_BIN": str(stub)}):
            note = menu_actions.instruct(headless, "Please retry the push")
        self.assertEqual(argv_file.read_text().splitlines(), ["queue", "--thread", "t2", "--message", "Please retry the push"])
        # No daemon answers here, so the menu may not claim the agent got it.
        self.assertTrue(note.startswith("queued, but"), note)
        self.assertEqual([(a["result"], a.get("delivery")) for a in self.m.actions()],
                         [("attempt", None), ("queued", "unknown")])

    def headless_codex(self, status, spawned):
        """A Codex agent with no pane, on a fake daemon whose queue behaves like the real one."""
        from test_peers import QUEUEING_STUB
        daemon = FakeCodexDaemon(self.m.sock)
        daemon.threads[THREAD] = {"id": THREAD, "name": "quiet", "cwd": "/w"}
        daemon.status[THREAD] = status
        if spawned:
            self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "quiet", "id": THREAD,
                                         "cwd": "/w", "access": "read-only", "approvals": "auto-review"})
        stub = self.m.dir / "codex-stub"
        stub.write_text(QUEUEING_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        agent = agent_state.Agent(key=f"codex:{THREAD}", kind="codex", name="quiet", session_id=THREAD,
                                  spawned=spawned)
        return daemon, agent

    def test_a_headless_spawned_codex_the_daemon_unloaded_is_woken_and_takes_the_instruction(self):
        daemon, agent = self.headless_codex("notLoaded", spawned=True)
        self.assertEqual(menu_actions.instruct(agent, "Please retry the push"), "queued, and a turn took it")
        self.assertEqual(daemon.taken, ["q-1"])
        (params,) = daemon.params("thread/resume")
        self.assertEqual((params["sandbox"], params["approvalPolicy"]), ("read-only", "on-request"))
        self.assertLess(daemon.methods().index("thread/resume"), daemon.methods().index("thread/queue/add"))
        self.assertEqual(self.m.actions()[-1]["delivery"], "started")

    def test_a_failed_check_after_the_queue_still_reports_queued_so_nothing_is_sent_twice(self):
        daemon, agent = self.headless_codex("idle", spawned=True)
        with mock.patch.object(menu_actions.codex_delivery, "confirm", side_effect=PermissionError("spawn log")):
            note, ok, _ = menu_actions.perform(agent, "Send", "Please retry the push")
        self.assertTrue(ok, note)                     # the popup clears the line: the text is queued
        self.assertTrue(note.startswith("queued, but"), note)
        self.assertEqual(len(daemon.params("thread/queue/add")), 1)

    def test_a_wake_that_failed_is_said_and_logged_not_swallowed(self):
        daemon, agent = self.headless_codex("notLoaded", spawned=True)
        with mock.patch.object(menu_actions.codex_delivery, "prepare", side_effect=RuntimeError("boom")), \
                mock.patch.object(menu_actions.codex_delivery, "wake", return_value=(False, "no wake")):
            note, ok, _ = menu_actions.perform(agent, "Send", "Please retry the push")
        self.assertTrue(ok, note)
        self.assertIn("RuntimeError: boom", note)
        self.assertEqual(self.m.actions()[-1]["wake_error"], "RuntimeError: boom")

    def test_a_torn_spawn_record_neither_crashes_the_popup_nor_blocks_the_send(self):
        daemon, agent = self.headless_codex("notLoaded", spawned=False)
        with open(self.m.dir / "spawned.jsonl", "wb") as fh:
            fh.write(b'{"event": "spawned", "kind": "codex", "id": "' + THREAD.encode() + b'", "name": "caf\xc3')
        note, ok, _ = menu_actions.perform(agent, "Send", "Please retry the push")
        self.assertTrue(ok, note)
        self.assertEqual(len(daemon.params("thread/queue/add")), 1)

    def test_a_headless_codex_that_is_not_ours_is_never_woken_and_the_menu_says_it_waits(self):
        daemon, agent = self.headless_codex("notLoaded", spawned=False)
        note = menu_actions.instruct(agent, "Please retry the push")
        self.assertIn("not loaded", note)
        self.assertNotIn("thread/resume", daemon.methods())
        self.assertEqual(daemon.queued_ids(), {THREAD: ["q-1"]})
        daemon.list_other_ids = True  # found by its text too, not only by the printed id
        self.assertIn("not loaded", menu_actions.instruct(agent, "A second instruction"))

    def test_attach_is_for_a_background_claude_with_no_pane_and_a_plain_id(self):
        def claude(**kw):
            base = dict(key="claude:s", kind="claude", name="far; away #(touch x)", session_id="s",
                        short_id="abcdef12", background=True)
            return agent_state.Agent(**dict(base, **kw))
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(claude(), "hello")                          # no pane: no typing
        for bad in (claude(kind="codex"), claude(background=False), claude(pane_id="%3"),
                    claude(short_id="abc; rm -rf ~"), claude(short_id="")):
            with self.assertRaises(menu_actions.Refused):
                menu_actions.attach(bad)
        self.assertEqual(self.acted(), [])
        self.assertEqual(menu_actions.attach(claude()), "opened in a new window")
        (call,) = self.acted()
        self.assertTrue(call.startswith("new-window -a -n far; away ##(touch x) "), call)  # after the current window; `#` doubled
        self.assertTrue(call.endswith(" attach abcdef12"), call)

    def test_only_a_live_spawned_agent_can_be_retired(self):
        for agent in (self.agent, agent_state.Agent(key="codex:x", kind="codex", name="x", session_id="x",
                                                    spawned=True, retired=True)):
            with self.assertRaises(menu_actions.Refused):
                menu_actions.retire(agent)
        self.assertEqual(self.m.actions(), [])

    def test_retire_runs_retire_peer_and_logs_both_lines(self):
        daemon = FakeCodexDaemon(self.m.sock)
        daemon.threads[THREAD] = {"id": THREAD, "name": "kid", "cwd": "/w"}
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "kid", "id": THREAD,
                                     "cwd": "/w", "access": "read-only", "spawned_by": "claude/x"})
        kid = agent_state.Agent(key=f"codex:{THREAD}", kind="codex", name="kid", session_id=THREAD, spawned=True)
        self.assertEqual(menu_actions.retire(kid), "retired")
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD}])
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt", "retired"])

    def test_open_pane_focuses_and_logs(self):
        self.assertEqual(menu_actions.open_pane(self.agent), "pane focused")
        # The client is switched first: select-window alone would not show a pane in another session.
        self.assertEqual(self.acted(), ["switch-client -t %7", "select-window -t %7", "select-pane -t %7"])
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt", "focused"])

    def test_a_pane_that_waits_on_the_owner_gets_no_keys(self):
        """Typed text lands on the prompt: Enter picks the highlighted answer, and Codex takes single
        letters as answers (a: yes for the whole session). So nothing is typed while a pane waits on
        the owner, whichever way that shows, and the check uses the pane as it is now."""
        menu = ("Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n"
                "  3. No, and tell Claude what to do differently (esc)\n")
        for label, extra, title, screen in (
                ("flagged in the popup's reading", {"needs_owner": 1}, "builder | proj", "› Ask Codex\n"),
                ("an Action Required title", {}, "[ . ] Action Required | builder | proj", "› Ask Codex\n"),
                ("an approval menu on screen", {}, "builder | proj", menu)):
            with self.subTest(label):
                self.m.panes = [(7, 700, "codex", title)]
                self.m.write()
                (self.m.dir / "screen-7.txt").write_text(screen)
                with self.assertRaises(menu_actions.Refused):
                    menu_actions.instruct(dataclasses.replace(self.agent, **extra), "wait, explain why first")
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys")], [])
        self.assertEqual({(a["action"], a["result"]) for a in self.m.actions()}, {("refused", "waiting for the owner")})

    def test_a_pane_in_copy_mode_or_with_synchronized_panes_gets_no_keys(self):
        """In copy mode, which scrolling back enters, the keys drive the mode and the text is lost.
        With synchronize-panes on, it would be typed into every pane of the window."""
        for flags, reason in (((True, False), "copy mode"), ((False, True), "synchronize-panes")):
            with self.subTest(reason):
                self.m.panes = [(7, 700, "codex", "builder | proj", *flags)]
                self.m.write()
                with self.assertRaisesRegex(menu_actions.Refused, reason):
                    menu_actions.instruct(self.agent, "rerun all specs")
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys")], [])
        self.m.panes = [(7, 700, "codex", "builder | proj", True, False)]
        self.m.write()
        self.assertEqual(menu_actions.open_pane(self.agent), "pane focused")       # looking at it is fine

    def test_a_claude_pane_is_checked_by_process_not_by_its_status_title(self):
        """Claude Code puts live status in its title, so the check before typing follows the process,
        as the match did: alive, under the pane, and the foreground of the pane's terminal."""
        self.m.pane(4, 400, "claude", "2 awaiting input · claude agents")
        self.m.process(500, 400, pgrp=500, tpgid=500)
        session = agent_state.Agent(key="claude:s", kind="claude", name="lead", session_id="s",
                                    pane_id="%4", pane_pid=400, pid=500)
        self.assertEqual(menu_actions.open_pane(session), "pane focused")
        self.assertEqual(menu_actions.instruct(session, "hello"), "typed into its pane")
        # Stopped with Ctrl+Z while another session runs in the same shell: still under the pane, but
        # not what it shows.
        self.m.process(500, 400, pgrp=500, tpgid=501)
        self.m.process(501, 400, pgrp=501, tpgid=501)
        with self.assertRaises(menu_actions.Refused):
            menu_actions.instruct(session, "hello again")
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys -t %4 -l")], ["send-keys -t %4 -l -- hello"])

    def test_enter_waits_until_the_typing_has_settled(self):
        """Codex reads an Enter that follows a fast burst of keys within 120 ms as a newline inside a
        paste, so an Enter sent right behind the text would leave the instruction unsubmitted."""
        self.assertGreater(ENTER_DELAY, 0.12)
        with mock.patch.object(tmux_src, "ENTER_DELAY", ENTER_DELAY), mock.patch.object(tmux_src.time, "sleep") as slept:
            menu_actions.instruct(self.agent, "hello")
        slept.assert_called_once_with(ENTER_DELAY)
        self.assertEqual([c for c in self.acted() if c.startswith("send-keys")],
                         ["send-keys -t %7 -l -- hello", "send-keys -t %7 Enter"])

    def test_a_failed_enter_is_not_reported_as_nothing_sent(self):
        """The text is already in the pane. 'Not done' would invite a second send, which doubles it."""
        wrapper = self.m.dir / "tmux-enter-fails"
        wrapper.write_text("#!/usr/bin/env bash\n[[ \"$*\" == \"send-keys -t %7 Enter\" ]] && "
                           f"{{ echo 'lost server' >&2; exit 1; }}\nexec {self.m.env['TMUX_BIN']} \"$@\"\n")
        wrapper.chmod(0o755)
        with mock.patch.dict(os.environ, {"TMUX_BIN": str(wrapper)}):
            note, ok, close = menu_actions.perform(self.agent, "Send", "run the tests")
        self.assertTrue(ok, note)                       # so the popup clears the line instead of keeping it
        self.assertIn("Enter failed", note)
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt", "typed, Enter failed"])

    def test_perform_acts_only_on_a_button_that_is_still_offered(self):
        """A key already on its way when a reload withdrew the button must not act. Here the agent was
        retired meanwhile, so Send would queue into its archived thread."""
        retired = agent_state.Agent(key="codex:t2", kind="codex", name="quiet", session_id="t2",
                                    spawned=True, retired=True)
        self.assertEqual(menu_actions.perform(retired, "Send", "hello"),
                         ("not done: that is no longer offered for this agent", False, False))
        self.assertFalse((self.m.dir / "codex-calls.txt").exists())
        self.assertEqual(self.m.actions(), [])


@unittest.skipUnless(shutil.which("tmux"), "needs a real tmux")
class RealTmuxFormatTest(unittest.TestCase):
    """tmux expands #{...} and runs #(shell) in a popup title and a window name. Session names are
    chosen by other agents, so this runs a real private tmux server to prove a name cannot run code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rt")
        self.addCleanup(self._tmp.cleanup)
        self.socket = f"agentmenu-test-{os.getpid()}"
        self.tmux = ["tmux", "-L", self.socket, "-f", "/dev/null"]
        subprocess.run(self.tmux + ["new-session", "-d", "-s", "t", "-x", "80", "-y", "24", "sleep 60"], check=True)
        self.addCleanup(self.stop_server)
        wrapper = Path(self._tmp.name) / "tmux-private"
        wrapper.write_text(f"#!/usr/bin/env bash\nexec tmux -L {self.socket} -f /dev/null \"$@\"\n")
        wrapper.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"TMUX_BIN": str(wrapper)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop_server(self):
        path = subprocess.run(self.tmux + ["display-message", "-p", "#{socket_path}"],
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(self.tmux + ["kill-server"], capture_output=True)
        if path and os.path.basename(path) == self.socket:
            try:
                os.unlink(path)                       # tmux leaves the socket file behind
            except OSError:
                pass

    def attach_client(self):
        """display-popup needs an attached client. A pty gives it one."""
        import pty
        import threading
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", self.tmux + ["attach", "-t", "t"])

        def drain():
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        self.addCleanup(lambda: os.close(fd))
        for _ in range(40):
            if subprocess.run(self.tmux + ["list-clients"], capture_output=True, text=True).stdout.strip():
                return
            time.sleep(0.1)
        self.skipTest("could not attach a client to the private tmux server")

    def test_a_hostile_popup_title_runs_nothing(self):
        self.attach_client()
        marker, control = Path(self._tmp.name) / "pwned-title", Path(self._tmp.name) / "control"
        tmux_src.popup("sleep 0.3", title=f"#(touch {marker}) #{{pane_pid}};")
        # The control proves this setup would have caught it: the same title, unescaped, does run.
        tmux_src._tmux("display-popup", "-E", "-T", f"#(touch {control})", "sleep 0.3", timeout=None)
        time.sleep(1.2)
        self.assertTrue(control.exists(), "the control did not run, so this test proves nothing")
        self.assertFalse(marker.exists(), "the popup title ran a shell command")

    def test_copy_mode_and_synchronized_panes_are_seen(self):
        pane = subprocess.run(self.tmux + ["list-panes", "-t", "t", "-F", "#{pane_id}"],
                              capture_output=True, text=True).stdout.split()[0]
        (listed,) = [p for p in tmux_src.list_panes() if p["pane_id"] == pane]
        self.assertEqual((listed["in_mode"], listed["synchronized"]), (False, False))
        subprocess.run(self.tmux + ["copy-mode", "-t", pane], check=True)
        subprocess.run(self.tmux + ["set-option", "-w", "-t", pane, "synchronize-panes", "on"], check=True)
        (listed,) = [p for p in tmux_src.list_panes() if p["pane_id"] == pane]
        self.assertEqual((listed["in_mode"], listed["synchronized"]), (True, True))

    def test_a_prompt_wider_than_its_pane_is_still_recognised(self):
        """tmux wraps a long prompt over two screen lines. Read unjoined, a pattern anchored to the
        line missed it, and the instruction line stayed on above a password prompt."""
        subprocess.run(self.tmux + ["new-session", "-d", "-s", "narrow", "-x", "36", "-y", "10",
                                    "printf \"Password for 'https://someone@github.example.com': \"; sleep 30"],
                       check=True)
        pane = subprocess.run(self.tmux + ["list-panes", "-t", "narrow", "-F", "#{pane_id}"],
                              capture_output=True, text=True).stdout.split()[0]
        for _ in range(40):
            screen = tmux_src.capture(pane)
            if "github" in screen:
                break
            time.sleep(0.05)
        self.assertEqual(auth_readers.detect_screen(screen), "password")

    def test_focus_brings_a_pane_in_another_session_to_the_client(self):
        self.attach_client()                                                   # the client shows session t
        subprocess.run(self.tmux + ["new-session", "-d", "-s", "u", "sleep 60"], check=True)
        pane = subprocess.run(self.tmux + ["list-panes", "-t", "u", "-F", "#{pane_id}"],
                              capture_output=True, text=True).stdout.split()[0]
        tmux_src.focus(pane)
        shown = subprocess.run(self.tmux + ["list-clients", "-F", "#{client_session}"],
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(shown, ["u"])

    def test_a_hostile_name_is_shown_as_text_and_runs_nothing(self):
        marker = Path(self._tmp.name) / "pwned"
        hostile = f"#(touch {marker}) #{{pane_pid}};"
        tmux_src.new_window(hostile, "sleep 30")
        names = subprocess.run(self.tmux + ["list-windows", "-a", "-F", "#{window_name}"],
                               capture_output=True, text=True).stdout
        self.assertIn("#(touch", names)                  # kept as literal text
        self.assertIn("#{pane_pid}", names)              # not expanded to a number
        time.sleep(1.2)                                  # a #() job would have run by now
        self.assertFalse(marker.exists(), "the window name ran a shell command")
        self.assertEqual(tmux_src.literal("a#b;"), "a##b\;")


if __name__ == "__main__":
    unittest.main()
