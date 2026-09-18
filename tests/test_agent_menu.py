"""Black-box tests for bin/agent-menu --once and bin/agent-menu-detail --print."""
import datetime
import importlib.machinery
import importlib.util
import os
import pty
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from menu_fixtures import BIN, FakeCodexDaemon, Machine

CLAUDE_A = "aaaaaaaa-0000-4000-8000-000000000001"
CLAUDE_B = "bbbbbbbb-0000-4000-8000-000000000002"
CODEX_P = "0d0d0d0d-0000-7000-8000-00000000000a"
CODEX_C = "0e0e0e0e-0000-7000-8000-00000000000b"


def stamp(minutes_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_script(name):
    """Import one of the bin/ programs as a module, without running it."""
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True     # no bytecode cache left in bin/
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


class AgentMenuTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="am")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        self.daemon = FakeCodexDaemon(self.m.sock)

    def codex_thread(self, thread_id, name, status="idle", listed=True):
        self.daemon.threads[thread_id] = {"id": thread_id, "name": name, "cwd": "/w", "status": {"type": status}}
        if not listed:
            self.daemon.unlisted = getattr(self.daemon, "unlisted", set()) | {thread_id}

    def tree(self, columns=34, **env):
        proc = subprocess.run([sys.executable, str(BIN / "agent-menu"), "--once", "--columns", str(columns)],
                              env=dict(self.m.env, **env), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.splitlines()

    def detail(self, key):
        return subprocess.run([sys.executable, str(BIN / "agent-menu-detail"), "--print", key],
                              env=self.m.env, capture_output=True, text=True, timeout=30)

    def test_names_kinds_and_states_are_shown(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500, status="busy")
        self.codex_thread(CODEX_P, "builder", status="idle")
        lines = self.tree()
        self.assertEqual(lines[0], " AGENTS  all quiet")
        self.assertTrue(any(l.startswith("   builder") and l.endswith("codex ✓") for l in lines), lines)
        self.assertTrue(any(l.startswith("   reviewer") and l.endswith("claude ⠋") for l in lines), lines)
        self.assertTrue(all(len(l) <= 34 for l in lines), lines)

    def test_a_spawned_agent_hangs_under_its_parent_with_its_access(self):
        self.m.claude_session(CLAUDE_A, "lead session", 500, status="busy")
        self.codex_thread(CODEX_C, "comms-test", status="active")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "comms-test", "id": CODEX_C,
                                     "access": "read-only", "spawned_by": "claude/lead-session [aaaaaaaa]"})
        lines = self.tree()
        parent = next(i for i, l in enumerate(lines) if "lead session" in l)
        self.assertTrue(lines[parent].startswith(" ▾ lead session"), lines)
        self.assertTrue(lines[parent + 1].startswith("   └ comms-test"), lines)
        self.assertTrue(lines[parent + 1].endswith("ro codex ⠋"), lines)
        text = self.detail(f"claude:{CLAUDE_A}").stdout
        self.assertIn("CHILDREN   comms-test  ro  ⠋ working", text)
        self.assertIn("spawned by: lead session", self.detail(f"codex:{CODEX_C}").stdout)

    def test_a_just_spawned_thread_missing_from_the_listing_is_still_shown(self):
        self.codex_thread(CODEX_C, "fresh-one", status="idle")
        self.daemon.hidden_from_list = {CODEX_C}
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "fresh-one", "id": CODEX_C,
                                     "access": "write", "spawned_by": "claude/nobody"})
        lines = self.tree()
        self.assertTrue(any("fresh-one" in l and l.endswith("rw codex ✓") for l in lines), lines)

    def test_a_retired_child_drops_off_after_ten_minutes(self):
        for thread_id, name, minutes in ((CODEX_P, "just-done", 2), (CODEX_C, "long-done", 30)):
            self.codex_thread(thread_id, name, status="notLoaded")
            self.m.log("spawned.jsonl",
                       {"event": "spawned", "kind": "codex", "name": name, "id": thread_id, "access": "read-only",
                        "spawned_by": "claude/x"},
                       {"event": "retired", "id": thread_id, "action": "archive", "time_utc": stamp(minutes)})
        lines = self.tree()
        self.assertTrue(any("just-done" in l and l.endswith("retired -") for l in lines), lines)  # words, not only dim
        self.assertFalse(any("long-done" in l for l in lines), lines)

    def test_unanswered_messages_and_needs_owner_are_counted(self):
        self.m.claude_session(CLAUDE_A, "blocked one", 500, status="waiting", state="blocked")
        self.codex_thread(CODEX_P, "builder")
        self.m.log("messages.jsonl",
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m1", "result": "queued", "first_line": "Review"},
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m2", "result": "queued", "first_line": "Again"},
                   {"channel": "receipt", "msg_id": "m1"},
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m3", "result": "queued", "first_line": "ACK m0"},
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m4", "result": "error", "first_line": "Failed"},
                   '{"torn": ')
        lines = self.tree()
        self.assertEqual(lines[0], " AGENTS  1 need you · 1 unanswered")
        self.assertTrue(lines[1].endswith("claude !1"), lines)   # the one that needs the owner sorts first
        self.assertTrue(any("builder" in l and l.endswith("codex ✉1") for l in lines), lines)

    def test_pane_title_and_question_count_mark_a_waiting_codex(self):
        self.codex_thread(CODEX_P, "builder", status="active")
        self.m.pane(7, 700, "codex", "[ . ] Action Required | builder | proj", screen="• Queued\n  ? 2 questions\n")
        self.assertTrue(any("builder" in l and l.endswith("codex !2") for l in self.tree()))
        text = self.detail(f"codex:{CODEX_P}").stdout
        self.assertIn("NEEDS YOU (2)", text)
        self.assertIn("buttons: Open pane\n", text)           # typed text would answer the prompt: no Send
        self.assertIn("typed text would answer its prompt", text)

    def test_an_auth_prompt_outranks_everything_and_turns_the_instruction_line_off(self):
        self.m.claude_session(CLAUDE_A, "pusher", 500, status="waiting", state="blocked")
        self.m.pane(3, 300, "claude", "pusher", screen="Pushing...\nEnter passphrase for key '/k/id_ed25519': ")
        self.m.process(500, 300)
        lines = self.tree()
        self.assertEqual(lines[0], " AGENTS  1 needs login")
        self.assertTrue(any("pusher" in l and l.endswith("claude ✗") for l in lines), lines)
        text = self.detail(f"claude:{CLAUDE_A}").stdout
        self.assertIn("ssh key passphrase", text)
        self.assertTrue(text.endswith("buttons: Open pane\n"), text)       # no Send
        self.assertNotIn("id_ed25519", text)               # the screen itself is never shown

    def buttons(self, key):
        return self.detail(key).stdout.strip().splitlines()[-1]

    def test_two_panes_for_one_agent_means_no_pane(self):
        self.codex_thread(CODEX_P, "twin")
        self.m.pane(1, 100, "codex", "twin | proj")
        self.m.pane(2, 200, "codex", "twin | proj")
        self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Send")     # queued, never typed into a guess

    def test_a_codex_thread_matches_its_name_segment_only_never_the_project(self):
        self.codex_thread(CODEX_P, "proj")            # named like the project folder of another agent's pane
        self.codex_thread(CODEX_C, "builder", status="active")
        self.m.pane(7, 700, "codex", "⠏ builder | proj")
        self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Send")
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Open pane, Send")
        self.m.panes = []
        self.m.pane(8, 800, "codex", "[ . ] Action Required | builder | proj")      # a status segment in front
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Open pane")    # matched, but waiting: no Send
        self.m.panes = []
        self.m.pane(9, 900, "codex", "my builder | proj")                           # a substring is not a match
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Send")

    def test_a_title_that_reads_two_ways_gives_the_pane_to_neither_agent(self):
        for full, part, title in (("a | b", "b", "a | b | proj"), ("- fix", "fix", "- fix | proj")):
            with self.subTest(title=title):
                self.daemon.threads.clear()
                self.m.panes = []
                self.codex_thread(CODEX_P, full, status="active")
                self.codex_thread(CODEX_C, part, status="idle")
                self.m.pane(1, 100, "codex", title)
                self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Send")
                self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Send")
                del self.daemon.threads[CODEX_C]                 # alone, the full name does get its pane
                self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Open pane, Send")

    def test_a_process_id_beats_a_doubtful_title_for_claude(self):
        self.m.claude_session(CLAUDE_A, "same name", 510, background=False)
        self.m.claude_session(CLAUDE_B, "same name", 520, background=False)
        self.m.pane(1, 100, "claude", "same name")
        self.m.pane(2, 200, "claude", "same name")
        self.m.process(510, 100)
        self.m.process(520, 200)
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane, Send")
        self.assertEqual(self.buttons(f"claude:{CLAUDE_B}"), "buttons: Open pane, Send")

    def test_a_pane_claimed_by_two_agents_goes_to_neither_and_a_stopped_thread_claims_nothing(self):
        self.codex_thread(CODEX_P, "builder", status="active")
        self.codex_thread(CODEX_C, "builder", status="notLoaded")                  # an old thread, same name
        self.m.pane(7, 700, "codex", "builder | proj")
        self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Open pane, Send")   # the live one keeps its pane
        self.daemon.threads[CODEX_C]["status"] = {"type": "idle"}                  # now two live agents fit one pane
        self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Send")
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Send")

    def test_the_pane_must_run_the_agents_own_cli(self):
        self.codex_thread(CODEX_P, "builder")
        self.m.pane(7, 700, "bash", "builder | proj")                              # any program can set a title
        self.assertEqual(self.buttons(f"codex:{CODEX_P}"), "buttons: Send")

    def test_a_session_started_inside_another_session_gets_no_pane(self):
        self.m.claude_session(CLAUDE_A, "lead", 110)                      # runs in the pane
        self.m.claude_session(CLAUDE_B, "helper", 120, cwd="/w")          # started by lead's Bash tool
        self.m.pane(1, 100, "claude", "lead")
        self.m.process(110, 100)
        self.m.process(120, 110)
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane, Send")
        self.assertEqual(self.buttons(f"claude:{CLAUDE_B}"), "buttons: Attach")   # no pane, so no typing

    def test_a_nested_session_with_the_same_name_gets_no_pane_either(self):
        self.m.claude_session(CLAUDE_A, "twin", 110)
        self.m.claude_session(CLAUDE_B, "twin", 120, cwd="/w")
        self.m.pane(1, 100, "claude", "twin")
        self.m.process(110, 100)
        self.m.process(120, 110)
        self.assertEqual(self.buttons(f"claude:{CLAUDE_B}"), "buttons: Attach")
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane, Send")   # the real one keeps it

    def test_a_detached_session_gets_no_pane_whatever_a_title_says(self):
        """A background session is detached and runs in no pane. Claude Code also rewrites its pane
        title with live status, so a title is never taken as proof for a Claude session."""
        self.m.claude_session(CLAUDE_A, "lead", 900, cwd="/w")
        self.m.pane(1, 100, "claude", "lead")                             # the title names it, and is ignored
        self.m.process(900, 1)                                            # detached, not under the pane
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Attach")

    def test_a_claude_pane_is_matched_by_process_even_when_the_title_is_a_status_line(self):
        self.m.claude_session(CLAUDE_A, "lead session", 500, status="busy")
        self.m.pane(4, 400, "claude", "2 awaiting input · claude agents")
        self.m.process(500, 400)
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane, Send")

    def test_a_hostile_session_name_is_neutralised_everywhere(self):
        self.m.claude_session(CLAUDE_A, "evil\x1b]0;title\x07\x1b[2Jname\nsecond\tline", 500)
        lines = self.tree()
        text = "\n".join(lines) + self.detail(f"claude:{CLAUDE_A}").stdout
        for bad in ("\x1b", "\x07", "\t"):
            self.assertNotIn(bad, text)
        row = next(l for l in lines if "evil" in l)
        self.assertTrue(row.endswith("claude ✓"), row)                             # the mark stays in its column
        self.assertLessEqual(len(row), 34)

    def test_folders_and_message_lines_from_other_agents_are_cleaned_too(self):
        nasty = "\x1b]52;c;ZXZpbA==\x07 \u202egnp.exe"
        self.m.claude_session(CLAUDE_A, "reviewer", 500, cwd="/w\x1b[2J" + nasty)
        self.m.log("messages.jsonl", {"channel": "claude socket", "thread_uuid": CLAUDE_A, "msg_id": "m1",
                                      "result": "written", "first_line": "hello" + nasty})
        text = self.detail(f"claude:{CLAUDE_A}").stdout + "\n".join(self.tree())
        self.assertIn("hello", text)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, text)

    def test_a_hostile_codex_thread_name_and_folder_are_cleaned(self):
        self.codex_thread(CODEX_P, "bad\x1b[2J\u202ename\ttab")
        self.daemon.threads[CODEX_P]["cwd"] = "/w\x1b]0;x\x07"
        text = "\n".join(self.tree()) + self.detail(f"codex:{CODEX_P}").stdout
        self.assertIn("bad?[2J?name?tab", text)
        for bad in ("\x1b", "\x07", "\u202e", "\t"):
            self.assertNotIn(bad, text)

    def test_a_forged_log_line_with_wrong_value_types_breaks_nothing(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.codex_thread(CODEX_P, "builder")
        self.m.claude.append({"sessionId": CLAUDE_B, "name": 123, "cwd": ["x"], "id": 7, "pid": "no", "kind": "background"})
        self.m.write()
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": ["n"], "id": CODEX_P,
                                     "access": "read-only", "spawned_by": 123},
                   {"event": "spawned", "kind": "codex", "id": ["x"], "spawned_by": {"a": 1}})
        self.m.log("messages.jsonl", {"channel": "codex queue", "thread_uuid": [CODEX_P], "msg_id": ["m"],
                                      "result": "queued", "first_line": 5},
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m2", "result": "queued", "first_line": 5})
        lines = self.tree()
        self.assertFalse(any("refresh failed" in l or "Traceback" in l for l in lines), lines)
        self.assertTrue(any("reviewer" in l for l in lines) and any("builder" in l for l in lines), lines)
        self.assertTrue(any(l.strip().startswith("bbbbbbbb") for l in lines), lines)      # a nameless session shows its id
        for key in (f"codex:{CODEX_P}", f"claude:{CLAUDE_B}"):
            proc = self.detail(key)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_boolean_or_deeply_nested_log_line_breaks_nothing(self):
        """JSON true where text belongs, or a line nested thousands of levels deep, is one more kind of
        forged line: it must not blank the tree or crash a popup."""
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.codex_thread(CODEX_P, "builder")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "builder", "id": CODEX_P,
                                     "access": "read-only", "spawned_by": True, "time_utc": True})
        self.m.log("messages.jsonl",
                   {"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": "m1", "result": "queued",
                    "first_line": True, "sender": True, "time_utc": True},
                   "[" * 5000 + "]" * 5000)
        lines = self.tree()
        self.assertFalse(any("refresh failed" in l for l in lines), lines)
        self.assertTrue(any("reviewer" in l for l in lines) and any("builder" in l for l in lines), lines)
        for key in (f"codex:{CODEX_P}", f"claude:{CLAUDE_A}"):
            proc = self.detail(key)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_codex_thread_that_reports_it_waits_on_the_owner_says_so_without_a_pane(self):
        self.codex_thread(CODEX_P, "builder", status="active")
        self.daemon.threads[CODEX_P]["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
        lines = self.tree()
        self.assertEqual(lines[0], " AGENTS  1 need you")
        self.assertTrue(any("builder" in l and l.endswith("codex !1") for l in lines), lines)
        text = self.detail(f"codex:{CODEX_P}").stdout
        self.assertIn("NEEDS YOU (1)", text)
        self.assertIn(f"codex resume {CODEX_P}", " ".join(text.split()))    # it has no pane to open
        self.assertTrue(text.endswith("buttons: Send\n"), text)             # queueing answers no prompt
        self.daemon.threads[CODEX_P]["status"] = {"type": "active", "activeFlags": []}
        self.assertTrue(any("builder" in l and l.endswith("codex ⠋") for l in self.tree()))   # working again

    def test_an_approval_menu_on_screen_means_needs_you_and_no_typing(self):
        """Typed text would answer the menu: Enter picks the highlighted Yes."""
        self.m.claude_session(CLAUDE_A, "lead", 500, background=False)
        self.m.pane(4, 400, "claude", "✳ lead", screen="Bash command\n  git push --force origin main\n"
                    "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again for git push commands\n"
                    "  3. No, and tell Claude what to do differently (esc)\n")
        self.m.process(500, 400)
        self.assertTrue(any("lead" in l and l.endswith("claude !1") for l in self.tree()))
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane")

    def test_a_numbered_answer_is_not_mistaken_for_an_approval_menu(self):
        self.m.claude_session(CLAUDE_A, "lead", 500, background=False)
        self.m.pane(4, 400, "claude", "✳ lead", screen="Summary:\n1. Yes, the tests pass.\n2. The build is green.\n")
        self.m.process(500, 400)
        self.assertEqual(self.buttons(f"claude:{CLAUDE_A}"), "buttons: Open pane, Send")

    def test_a_finished_spawned_claude_agent_stays_under_its_parent(self):
        """A spawned Claude agent that finished drops out of `claude agents --json` but is still
        spawn-peer's to retire, so it is looked up in `--all`. Other finished sessions stay out."""
        self.codex_thread(CODEX_P, "lead thread")
        done = {"pid": None, "status": "idle", "state": "done", "cwd": "/w", "kind": "background"}
        self.m.claude_finished += [dict(done, sessionId=CLAUDE_B, id="bbbbbbbb", name="review"),
                                   dict(done, sessionId=CLAUDE_A, id="aaaaaaaa", name="old history")]
        self.m.write()
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "claude", "name": "review", "id": "bbbbbbbb",
                                     "access": "read-only", "spawned_by": "codex/lead-thread"})
        lines = self.tree()
        parent = next(i for i, l in enumerate(lines) if "lead thread" in l)
        self.assertTrue(lines[parent + 1].startswith("   └ review"), lines)
        self.assertFalse(any("old history" in l for l in lines), lines)
        self.assertTrue(self.buttons(f"claude:{CLAUDE_B}").endswith("Retire"))

    def test_finished_sessions_are_looked_up_again_only_when_needed(self):
        """Every `claude` start costs CPU and the listing runs every two seconds, so `--all` runs again
        only when another spawned id goes missing, or once the lookup is a minute old."""
        sys.path.insert(0, str(BIN.parent / "lib"))
        from sources import claude_src
        calls = []

        def listing(extra_args=(), timeout=1.5):
            calls.append(tuple(extra_args))
            return [] if not extra_args else [{"short_id": "aa", "session_id": "A"}]
        self.addCleanup(claude_src._finished.update, missing=frozenset(), at=float("-inf"), sessions=[])
        claude_src._finished.update(missing=frozenset(), at=float("-inf"), sessions=[])
        with mock.patch.object(claude_src, "_listing", side_effect=listing):
            for _ in range(3):
                self.assertEqual([s["short_id"] for s in claude_src.list_sessions(extra_ids=["aa"])], ["aa"])
            self.assertEqual(calls.count(("--all",)), 1)
            claude_src.list_sessions(extra_ids=["aa", "bb"])                 # another one went missing
            self.assertEqual(calls.count(("--all",)), 2)
            with mock.patch.object(claude_src.time, "monotonic", return_value=time.monotonic() + 61):
                claude_src.list_sessions(extra_ids=["aa", "bb"])             # the lookup is a minute old
            self.assertEqual(calls.count(("--all",)), 3)

    def test_the_popup_gets_the_sidebars_settings(self):
        """tmux starts a popup with the tmux server's environment, not the sidebar's. The sidebar hands
        its settings over on the popup's command line, so look-only mode survives that step."""
        menu = load_script("agent-menu")
        environ = {"AGENT_MENU_LOOK_ONLY": "1", "AGENT_COMMS_LOG": "/tmp/a b/log", "UNRELATED": "x"}
        words = shlex.split(menu.detail_command("codex:abc", environ))
        self.assertEqual(words[0], "env")
        self.assertIn("AGENT_MENU_LOOK_ONLY=1", words)
        self.assertIn("AGENT_COMMS_LOG=/tmp/a b/log", words)
        self.assertNotIn("UNRELATED=x", words)
        self.assertEqual(words[-3:], [sys.executable, os.path.join(menu.BIN, "agent-menu-detail"), "codex:abc"])

    def test_a_failed_refresh_keeps_the_popup_and_says_so(self):
        """A source that could not be read must not look like the agent is gone: that closed the popup
        and threw away the instruction being typed."""
        detail = load_script("agent-menu-detail")
        last = ("the agent", "", [], "", [])
        with mock.patch.object(detail, "load", return_value=(None, "", [], "", ["claude: unreachable"])):
            kept, note = detail.reload("claude:x", last)
        self.assertIs(kept, last)
        self.assertIn("claude: unreachable", note)
        with mock.patch.object(detail, "load", return_value=(None, "", [], "", [])):   # read fine: really gone
            gone, note = detail.reload("claude:x", last)
        self.assertEqual((gone[0], note), (None, ""))

    def test_a_mouse_report_that_is_no_click_is_no_event(self):
        """The release after a held press arrives as a mouse report of its own. The popup reads past
        it, so it neither confirms an armed Retire nor cancels one. The next real key comes through."""
        import curses
        detail = load_script("agent-menu-detail")
        keys = iter([curses.KEY_MOUSE, "y"])
        screen = mock.Mock(get_wch=lambda: next(keys))
        with mock.patch.object(detail.menu_curses, "click", return_value=None):
            self.assertEqual(detail.read_event(screen), ("char", "y"))

    def test_a_popup_too_small_to_show_its_buttons_acts_on_no_key(self):
        """A popup too small for its buttons draws only "too small": no buttons, no confirm hint. The
        keys that retire an agent in a full-size popup, r and then Enter, must do nothing there."""
        self.codex_thread(CODEX_C, "kid")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "kid", "id": CODEX_C,
                                     "cwd": "/w", "access": "read-only", "spawned_by": "claude/x"})
        env = dict(self.m.env, TERM="xterm-256color", LINES="8", COLUMNS="98")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(BIN / "agent-menu-detail"), f"codex:{CODEX_C}"], env)
        shown = bytearray()

        def drain():
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                shown.extend(chunk)
        threading.Thread(target=drain, daemon=True).start()
        try:
            deadline = time.monotonic() + 20
            while b"too small" not in shown and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn(b"too small", shown)
            for key, pause in ((b"r", 0.8), (b"\r", 1.0), (b"q", 0.0)):
                os.write(fd, key)
                time.sleep(pause)
            deadline, ended = time.monotonic() + 10, False
            while not ended and time.monotonic() < deadline:
                ended = os.waitpid(pid, os.WNOHANG)[0] == pid
                time.sleep(0.05)
            self.assertTrue(ended, "q did not close the popup")
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)
        self.assertNotIn("thread/archive", self.daemon.methods())
        self.assertEqual(self.m.actions(), [])

    def test_a_locale_that_is_not_installed_stops_neither_program(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.assertTrue(any("reviewer" in l for l in self.tree(LC_ALL="xx_XX.UTF-8")))
        proc = subprocess.run([sys.executable, str(BIN / "agent-menu-detail"), "--print", f"claude:{CLAUDE_A}"],
                              env=dict(self.m.env, LC_ALL="xx_XX.UTF-8"), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_name_of_only_spaces_falls_back_to_the_id(self):
        self.codex_thread(CODEX_P, "   ")
        self.assertTrue(any(l.strip().startswith(CODEX_P[:8]) for l in self.tree()), self.tree())

    def test_a_refresh_that_blows_up_becomes_a_failure_line_not_an_old_tree(self):
        sys.path.insert(0, str(BIN.parent / "lib"))
        import agent_state
        from unittest import mock
        with mock.patch.object(agent_state, "collect", side_effect=TypeError("boom")):
            snapshot = agent_state.safe_collect()
        self.assertEqual((snapshot.roots, snapshot.by_key, snapshot.errors), ([], {}, ["refresh failed: TypeError"]))

    def test_a_label_without_a_kind_or_id_gives_no_parent(self):
        self.m.claude_session(CLAUDE_A, "owner main", 500)
        self.codex_thread(CODEX_P, "kid-one")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "kid-one", "id": CODEX_P,
                                     "access": "read-only", "spawned_by": "Owner_MAIN"})
        lines = self.tree()
        self.assertTrue(any(l.startswith("   owner main") for l in lines), lines)   # a leaf: nothing hung on it
        self.assertTrue(any(l.startswith("   kid-one") for l in lines), lines)      # top level, still shown
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "kid-one", "id": CODEX_P,
                                     "access": "read-only", "spawned_by": "claude/owner-main"})
        self.assertTrue(any(l.startswith(" ▾ owner main") for l in self.tree()))    # with its kind, it links

    def test_a_loop_in_the_records_hides_nobody(self):
        self.codex_thread(CODEX_P, "kid-one")
        self.codex_thread(CODEX_C, "kid-two")
        self.m.log("spawned.jsonl",
                   {"event": "spawned", "kind": "codex", "name": "kid-two", "id": CODEX_C, "access": "read-only",
                    "spawned_by": "codex/kid-one"},
                   {"event": "spawned", "kind": "codex", "name": "kid-one", "id": CODEX_P, "access": "read-only",
                    "spawned_by": "codex/kid-two"})
        lines = self.tree()
        for name in ("kid-one", "kid-two"):
            self.assertEqual(sum(name in l for l in lines), 1, lines)

    def test_a_deeply_nested_agent_is_drawn_and_counted(self):
        ids = [f"0f0f0f0f-0000-7000-8000-00000000000{i}" for i in range(4)]
        for i, thread_id in enumerate(ids):
            self.codex_thread(thread_id, f"level{i}")
            if i:
                self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": f"level{i}", "id": thread_id,
                                             "access": "read-only", "spawned_by": f"codex/level{i - 1}"})
        self.m.log("messages.jsonl", {"channel": "codex queue", "thread_uuid": ids[3], "msg_id": "m1",
                                      "result": "queued", "first_line": "x"})
        lines = self.tree(columns=40)
        self.assertEqual(lines[0], " AGENTS  1 unanswered")
        self.assertTrue(any("level3" in l and l.endswith("✉1") for l in lines), lines)

    def test_an_unreadable_or_damaged_log_is_an_error_line_not_a_crash(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        (self.m.dir / "spawned.jsonl").mkdir()                                     # cannot be read as a file
        (self.m.dir / "messages.jsonl").write_bytes(b'\xff\xfe not utf-8\n{"channel": "x"}\n')
        lines = self.tree()
        self.assertIn(" spawn log: unreadable", lines)
        self.assertTrue(any("reviewer" in l for l in lines), lines)

    def test_a_retire_time_in_the_future_does_not_keep_a_row_forever(self):
        self.codex_thread(CODEX_C, "time-traveller", status="notLoaded")
        self.m.log("spawned.jsonl",
                   {"event": "spawned", "kind": "codex", "name": "time-traveller", "id": CODEX_C,
                    "access": "read-only", "spawned_by": "claude/x"},
                   {"event": "retired", "id": CODEX_C, "action": "archive", "time_utc": "2999-01-01T00:00:00Z"})
        self.assertFalse(any("time-traveller" in l for l in self.tree()))

    def test_a_retired_agent_offers_no_buttons(self):
        self.codex_thread(CODEX_C, "just-done", status="notLoaded")
        self.m.log("spawned.jsonl",
                   {"event": "spawned", "kind": "codex", "name": "just-done", "id": CODEX_C, "access": "read-only",
                    "spawned_by": "claude/x"},
                   {"event": "retired", "id": CODEX_C, "action": "archive", "time_utc": stamp(1)})
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons:")

    def test_a_background_claude_with_no_pane_offers_attach_only(self):
        self.m.claude_session(CLAUDE_B, "far away", 900)
        text = self.detail(f"claude:{CLAUDE_B}").stdout
        self.assertIn("buttons: Attach\n", text)
        self.assertIn("use Attach", text)

    def test_a_dead_source_shows_an_error_line_not_stale_rows(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.codex_thread(CODEX_P, "builder")
        lines = self.tree(STUB_CLAUDE_FAIL="1")
        self.assertIn(" claude: unreachable", lines)
        self.assertFalse(any("reviewer" in l for l in lines))
        self.assertTrue(any("builder" in l for l in lines))
        lines = self.tree(CODEX_APP_SERVER_SOCK=self.m.sock + ".none", STUB_TMUX_FAIL="1")
        self.assertIn(" codex: unreachable", lines)
        self.assertIn(" tmux: unreachable", lines)
        self.assertTrue(any("reviewer" in l for l in lines))

    def test_the_header_shrinks_to_marks_when_words_do_not_fit(self):
        self.m.claude_session(CLAUDE_A, "blocked one", 500, status="waiting", state="blocked")
        self.codex_thread(CODEX_P, "builder")
        self.m.log("messages.jsonl", *[{"channel": "codex queue", "thread_uuid": CODEX_P, "msg_id": f"m{i}",
                                        "result": "queued", "first_line": "x"} for i in range(11)])
        self.assertEqual(self.tree()[0], " AGENTS  !1  ✉11")
        self.assertEqual(self.tree(columns=60)[0], " AGENTS  1 need you · 11 unanswered")

    def test_ascii_mode_uses_no_character_outside_ascii(self):
        self.m.claude_session(CLAUDE_A, "lead session with a long name", 500, status="busy")
        self.codex_thread(CODEX_C, "comms-test", status="active")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "comms-test", "id": CODEX_C,
                                     "access": "read-only", "spawned_by": "claude/lead-session-with-a-long-name"})
        self.m.log("messages.jsonl", {"channel": "codex queue", "thread_uuid": CODEX_C, "msg_id": "m1",
                                      "result": "queued", "first_line": "x"})
        lines = self.tree(AGENT_MENU_ASCII="1")
        for line in lines:
            self.assertTrue(line.isascii(), line)
        self.assertTrue(any(l.endswith("ro codex +1") for l in lines), lines)

    def test_a_long_answer_is_wrapped_in_full_never_cut(self):
        self.codex_thread(CODEX_P, "quiet")
        self.daemon.threads[CODEX_P]["turns"] = [{"status": "completed", "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "word " * 60 + "THE-END"}]}]
        text = self.detail(f"codex:{CODEX_P}").stdout
        self.assertIn("LATEST ANSWER", text)
        self.assertIn("THE-END", text)
        self.assertTrue(all(len(l) <= 96 for l in text.splitlines()), text)

    def test_an_auth_failure_in_a_headless_answer_marks_the_agent(self):
        self.codex_thread(CODEX_P, "quiet")
        self.daemon.threads[CODEX_P]["turns"] = [{"status": "completed", "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "push failed: Permission denied (publickey)."}]}]
        text = self.detail(f"codex:{CODEX_P}").stdout
        self.assertIn("NEEDS LOGIN", text)
        self.assertIn("ssh key rejected", text)
        # It has no pane to type a secret into, and `codex queue` types into nothing: after logging in,
        # the owner tells it to try again from here, so the instruction line stays on.
        self.assertNotIn("Open its pane", text)
        self.assertIn("Log in from your own terminal", text)
        self.assertTrue(text.endswith("buttons: Send\n"), text)

    def test_popup_sentences_wrap_at_words_for_any_width(self):
        sys.path.insert(0, str(BIN.parent / "lib"))
        import agent_state
        import menu_detail
        waiting = agent_state.Agent(key="k", kind="claude", name="n", session_id="s", needs_owner=1, background=True)
        locked = agent_state.Agent(key="k", kind="codex", name="n", session_id="s", auth="ssh key passphrase", pane_id="%1")
        for agent in (waiting, locked):
            for columns in (30, 56, 96):
                lines = [text for text, _ in menu_detail.body(agent, [], "", columns)]
                self.assertTrue(all(len(line) <= columns for line in lines), (columns, lines))
                words = " ".join(lines).split()
                self.assertIn("pane", words)                      # whole words survive: none is split

    def test_without_a_real_terminal_the_programs_say_so_instead_of_hanging(self):
        for tool, extra in (("agent-menu", []), ("agent-menu-detail", [f"claude:{CLAUDE_A}"])):
            proc = subprocess.run([sys.executable, str(BIN / tool), *extra], env=dict(self.m.env, TERM="dumb"),
                                  capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("real terminal", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_look_only_mode_offers_no_typing_and_no_retire(self):
        self.codex_thread(CODEX_C, "comms-test", status="active")
        self.m.pane(7, 700, "codex", "comms-test | proj")
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "comms-test", "id": CODEX_C,
                                     "access": "read-only", "spawned_by": "claude/x"})
        self.assertEqual(self.buttons(f"codex:{CODEX_C}"), "buttons: Open pane, Send, Retire")
        self.m.env["AGENT_MENU_LOOK_ONLY"] = "1"
        text = self.detail(f"codex:{CODEX_C}").stdout
        self.assertTrue(text.strip().endswith("buttons: Open pane"), text)
        self.assertIn("look-only mode", text)

    def test_refreshing_only_reads(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.codex_thread(CODEX_P, "builder")
        self.m.pane(7, 700, "codex", "builder | proj")
        self.tree()
        self.assertTrue(set(self.daemon.methods()) <= {"initialize", "initialized", "thread/list", "thread/read"})
        verbs = {call.split()[0] for call in self.m.tmux_calls()}
        self.assertTrue(verbs <= {"list-panes", "capture-pane"}, verbs)
        self.assertEqual(self.m.actions(), [])


if __name__ == "__main__":
    unittest.main()
