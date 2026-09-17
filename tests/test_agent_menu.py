"""Black-box tests for bin/agent-menu --once and bin/agent-menu-detail --print."""
import datetime
import subprocess
import sys
import tempfile
import unittest

from menu_fixtures import BIN, FakeCodexDaemon, Machine

CLAUDE_A = "aaaaaaaa-0000-4000-8000-000000000001"
CLAUDE_B = "bbbbbbbb-0000-4000-8000-000000000002"
CODEX_P = "0d0d0d0d-0000-7000-8000-00000000000a"
CODEX_C = "0e0e0e0e-0000-7000-8000-00000000000b"


def stamp(minutes_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        self.assertIn("buttons: Open pane, Send", text)

    def test_an_auth_prompt_outranks_everything_and_turns_the_instruction_line_off(self):
        self.m.claude_session(CLAUDE_A, "pusher", 500, status="waiting", state="blocked")
        self.m.pane(3, 300, "claude", "pusher", screen="Pushing...\nEnter passphrase for key '/k/id_ed25519': ")
        self.m.process(500, 300)
        lines = self.tree()
        self.assertEqual(lines[0], " AGENTS  1 needs login")
        self.assertTrue(any("pusher" in l and l.endswith("claude ✗") for l in lines), lines)
        text = self.detail(f"claude:{CLAUDE_A}").stdout
        self.assertIn("ssh key passphrase", text)
        self.assertIn("buttons: Open pane\n", text)       # no Send
        self.assertNotIn("id_ed25519", text)               # the screen itself is never shown

    def test_a_doubtful_pane_match_means_no_pane(self):
        self.codex_thread(CODEX_P, "twin")
        self.m.pane(1, 100, "codex", "twin")
        self.m.pane(2, 200, "codex", "twin")
        self.assertIn("buttons: Send", self.detail(f"codex:{CODEX_P}").stdout)   # queued, never typed into a guess

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

    def test_refreshing_only_reads(self):
        self.m.claude_session(CLAUDE_A, "reviewer", 500)
        self.codex_thread(CODEX_P, "builder")
        self.m.pane(7, 700, "codex", "builder")
        self.tree()
        self.assertTrue(set(self.daemon.methods()) <= {"initialize", "initialized", "thread/list", "thread/read"})
        verbs = {call.split()[0] for call in self.m.tmux_calls()}
        self.assertTrue(verbs <= {"list-panes", "capture-pane"}, verbs)
        self.assertEqual(self.m.actions(), [])


if __name__ == "__main__":
    unittest.main()
