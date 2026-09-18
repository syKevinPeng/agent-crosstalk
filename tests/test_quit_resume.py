"""Tests for quitting an agent and resuming it later. Nothing here deletes anything: Codex threads
are archived and unarchived, Claude background sessions are stopped and attached again."""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from menu_fixtures import LIB, FakeCodexDaemon, Machine

sys.path.insert(0, str(LIB))
import agent_state  # noqa: E402
import menu_actions  # noqa: E402
import menu_detail  # noqa: E402
import menu_render  # noqa: E402
from sources import claude_src  # noqa: E402

LIVE_CLAUDE = "aaaaaaaa-0000-4000-8000-000000000001"
DONE_CLAUDE = "bbbbbbbb-0000-4000-8000-000000000002"
LIVE_CODEX = "0d0d0d0d-0000-7000-8000-00000000000a"
ARCHIVED_CODEX = "0e0e0e0e-0000-7000-8000-00000000000b"


def spawn_stamp(minutes_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class QuitResumeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qr")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.daemon = FakeCodexDaemon(self.m.sock)
        self.m.claude_session(LIVE_CLAUDE, "busy one", 500, status="busy")
        self.m.quit_claude_session(DONE_CLAUDE, "parked one", cwd="/w")
        self.thread(LIVE_CODEX, "idle thread")
        self.thread(ARCHIVED_CODEX, "archived thread")
        self.daemon.archived.add(ARCHIVED_CODEX)

    def thread(self, thread_id, name, status="idle"):
        self.daemon.threads[thread_id] = {"id": thread_id, "name": name, "cwd": self.m.dir.as_posix(),
                                          "status": {"type": status}}

    def snap(self, include_quit=True):
        return agent_state.collect(include_quit=include_quit)

    def agent(self, key, include_quit=True):
        return self.snap(include_quit).by_key[key]

    def acted(self):
        return [c for c in self.m.tmux_calls() if not c.startswith(("list-panes", "capture-pane", "display-message"))]

    # ---------- what counts as quit

    def test_quit_agents_are_listed_apart_and_only_when_asked(self):
        hidden = self.snap(include_quit=False)
        self.assertEqual(hidden.quit_agents, [])
        self.assertNotIn(f"claude:{DONE_CLAUDE}", hidden.by_key)
        shown = self.snap()
        self.assertEqual([a.name for a in shown.quit_agents], ["archived thread", "parked one"])
        self.assertEqual(sorted(a.name for a in shown.roots), ["busy one", "idle thread"])
        self.assertTrue(all(a.quit and a.state == "stopped" for a in shown.quit_agents))

    def test_a_running_session_whose_last_task_is_done_is_not_quit(self):
        """`state: done` only means the last task finished. The CLI's active list decides."""
        self.m.claude[0]["state"] = "done"
        self.m.write()
        self.assertFalse(self.agent(f"claude:{LIVE_CLAUDE}").quit)

    def test_a_session_listed_twice_is_live_when_either_listing_is_live(self):
        self.m.claude_finished.append(dict(self.m.claude[0], state="done", pid=None))   # an earlier finished run
        self.m.write()
        sessions = {s["session_id"]: s for s in claude_src.list_sessions(include_quit=True)}
        self.assertFalse(sessions[LIVE_CLAUDE]["quit"])
        self.assertEqual(sessions[LIVE_CLAUDE]["pid"], 500)

    def test_a_stopped_session_never_carries_a_process_id(self):
        """If the CLI ever reports a stale pid for a stopped session, it must not be kept: a reused pid
        could otherwise match some other process to a pane."""
        self.m.claude_finished[0]["pid"] = 500
        self.m.write()
        parked = [s for s in claude_src.list_sessions(include_quit=True) if s["session_id"] == DONE_CLAUDE]
        self.assertEqual(parked[0]["pid"], None)

    def test_the_menus_own_usage_call_is_not_an_agent(self):
        """The limits block runs `claude -p /usage` in a folder of its own. While it runs, and after it
        ends, the CLI lists it like any session, so it would flicker into the tree as a working agent."""
        folder = os.path.join(self._tmp.name, "usage-calls")
        os.makedirs(folder)
        os.environ["AGENT_MENU_USAGE_CWD"] = folder
        self.m.claude_session("cccccccc-0000-4000-8000-000000000003", "usage-calls-78", 700, status="busy", cwd=folder)
        self.m.quit_claude_session("dddddddd-0000-4000-8000-000000000004", "usage-calls-77", cwd=folder + "/")
        self.m.write()
        names = [s["name"] for s in claude_src.list_sessions(include_quit=True)]
        self.assertNotIn("usage-calls-78", names)
        self.assertNotIn("usage-calls-77", names)
        self.assertIn("busy one", names)

    def test_quit_agents_never_count_in_the_header(self):
        self.m.log("messages.jsonl", {"channel": "codex queue", "thread_uuid": ARCHIVED_CODEX, "msg_id": "m1",
                                      "result": "queued", "first_line": "x"})
        self.assertEqual(menu_render.header(self.snap(), 60), " AGENTS  all quiet")

    # ---------- how quit agents are drawn

    def test_quit_rows_sit_under_a_divider_below_the_tree(self):
        rows = menu_render.rows(self.snap(), 34)
        styles = [r["style"] for r in rows]
        divider = styles.index("quitdiv")
        self.assertIn("QUIT", rows[divider]["text"])
        self.assertEqual(styles[divider + 1:], ["quit", "quit"])
        self.assertTrue(all(r["text"].rstrip().endswith("quit -") for r in rows[divider + 1:]), rows)
        self.assertEqual(menu_render.key_at(rows, divider), "")          # the divider is not clickable

    def test_only_the_leading_rows_stay_pinned_while_scrolling(self):
        rows = menu_render.rows(self.snap(), 34)
        self.assertEqual(menu_render.pinned_count(rows), 1)             # the header, not the divider
        last = rows[-1]["key"]
        top = menu_render.scroll_top(rows, last, 2, 0)
        body = [r["key"] for r in rows[menu_render.pinned_count(rows):]]
        self.assertIn(last, body[top:top + 2])

    # ---------- what the popup offers

    def test_the_buttons_follow_what_can_be_quit_and_resumed(self):
        cases = {f"codex:{LIVE_CODEX}": ["Send", "Quit agent"],
                 f"claude:{LIVE_CLAUDE}": ["Attach", "Quit agent"],
                 f"codex:{ARCHIVED_CODEX}": ["Resume"],
                 f"claude:{DONE_CLAUDE}": ["Resume"]}
        snap = self.snap()
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(menu_detail.buttons(snap.by_key[key]), expected)

    def test_nothing_in_a_pane_and_no_interactive_session_is_offered_quit(self):
        interactive = "cccccccc-0000-4000-8000-000000000003"
        self.m.claude_session(interactive, "at a terminal", 600, background=False)
        self.m.pane(7, 700, "codex", "idle thread | proj")
        snap = self.snap()
        for key, reason in ((f"claude:{interactive}", "terminal"), (f"codex:{LIVE_CODEX}", "pane")):
            with self.subTest(key=key):
                agent = snap.by_key[key]
                self.assertNotIn("Quit agent", menu_detail.buttons(agent))
                self.assertIn(reason, menu_detail.quit_note(agent))

    def test_the_confirm_says_what_happens_and_warns_when_busy(self):
        text = menu_detail.confirm_text(self.agent(f"claude:{LIVE_CLAUDE}"))
        self.assertIn("stop it", text)
        self.assertIn("Resume brings it back", text)
        self.assertIn("working right now", text)
        self.assertIn("archive it", menu_detail.confirm_text(self.agent(f"codex:{LIVE_CODEX}")))

    # ---------- quit

    def test_quitting_a_codex_thread_archives_it_by_id(self):
        note = menu_actions.quit_agent(self.agent(f"codex:{LIVE_CODEX}"))
        self.assertIn("archived", note)
        self.assertEqual(self.daemon.params("thread/archive"), [{"threadId": LIVE_CODEX}])
        self.assertEqual([(a["action"], a["result"]) for a in self.m.actions()],
                         [("quit", "attempt"), ("quit", "archived")])
        self.assertTrue(self.agent(f"codex:{LIVE_CODEX}").quit)            # it moved to the quit list

    def test_quitting_a_claude_session_stops_it(self):
        note = menu_actions.quit_agent(self.agent(f"claude:{LIVE_CLAUDE}"))
        self.assertIn("stopped", note)
        self.assertIn(f"stop {LIVE_CLAUDE[:8]}", self.m.claude_calls())

    def test_quit_is_refused_when_the_id_now_names_another_agent(self):
        agent = self.agent(f"codex:{LIVE_CODEX}")
        self.daemon.threads[LIVE_CODEX]["name"] = "someone else"
        with self.assertRaises(menu_actions.Refused):
            menu_actions.quit_agent(agent)
        claude = self.agent(f"claude:{LIVE_CLAUDE}")
        self.m.claude[0]["name"] = "renamed"
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.quit_agent(claude)
        self.assertEqual(self.daemon.params("thread/archive"), [])
        self.assertFalse(any(c.startswith("stop") for c in self.m.claude_calls()))
        self.assertEqual(self.m.actions(), [])

    def test_quit_is_refused_for_panes_interactive_sessions_and_the_already_quit(self):
        in_pane = agent_state.Agent(key="codex:p", kind="codex", name="p", session_id=LIVE_CODEX, pane_id="%7")
        interactive = agent_state.Agent(key="claude:i", kind="claude", name="i", session_id="i")
        for agent in (in_pane, interactive, self.agent(f"codex:{ARCHIVED_CODEX}")):
            with self.subTest(agent=agent.name), self.assertRaises(menu_actions.Refused):
                menu_actions.quit_agent(agent)
        self.assertEqual(self.m.actions(), [])

    def test_a_failed_stop_is_logged_and_reported(self):
        with mock.patch.dict(os.environ, {"STUB_STOP_FAIL": "1"}):
            with self.assertRaises(menu_actions.Refused):
                menu_actions.quit_agent(self.agent(f"claude:{LIVE_CLAUDE}"))
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt", "error"])

    def test_quit_needs_the_log_first(self):
        blocked = self.m.dir / "a-folder"
        blocked.mkdir()
        with mock.patch.dict(os.environ, {"AGENT_MENU_ACTIONS_LOG": str(blocked)}):
            with self.assertRaises(menu_actions.Refused):
                menu_actions.quit_agent(self.agent(f"codex:{LIVE_CODEX}"))
        self.assertEqual(self.daemon.params("thread/archive"), [])

    # ---------- resume

    def test_resuming_a_codex_thread_unarchives_it_and_opens_it_after_the_current_window(self):
        self.assertEqual(menu_actions.resume(self.agent(f"codex:{ARCHIVED_CODEX}")), "resumed in a new window")
        self.assertEqual(self.daemon.params("thread/unarchive"), [{"threadId": ARCHIVED_CODEX}])
        (call,) = self.acted()
        self.assertTrue(call.startswith("new-window -a -n archived thread -c "), call)
        self.assertTrue(call.endswith(f"resume {ARCHIVED_CODEX}"), call)
        self.assertFalse(self.agent(f"codex:{ARCHIVED_CODEX}").quit)       # back in the tree

    def test_resuming_a_claude_session_attaches_it(self):
        self.assertEqual(menu_actions.resume(self.agent(f"claude:{DONE_CLAUDE}")), "resumed in a new window")
        (call,) = self.acted()
        self.assertTrue(call.startswith("new-window -a -n parked one "), call)
        self.assertTrue(call.endswith(f"attach {DONE_CLAUDE[:8]}"), call)
        self.assertEqual([a["result"] for a in self.m.actions()], ["attempt", "attached in a new window"])

    def test_a_spawned_agent_can_be_quit_resumed_and_quit_again(self):
        """Resume must be recorded in the spawn record, or the menu and retire-peer would keep treating
        the agent as retired: no buttons, hidden from the tree, and a second quit refused."""
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "idle thread", "id": LIVE_CODEX,
                                     "cwd": self.m.dir.as_posix(), "access": "read-only", "spawned_by": "claude/x"})
        menu_actions.quit_agent(self.agent(f"codex:{LIVE_CODEX}"))          # through retire-peer
        self.assertTrue(self.agent(f"codex:{LIVE_CODEX}").quit)
        menu_actions.resume(self.agent(f"codex:{LIVE_CODEX}"))
        again = self.agent(f"codex:{LIVE_CODEX}")
        self.assertEqual((again.quit, again.retired), (False, False))
        self.assertIn("Quit agent", menu_detail.buttons(again))
        menu_actions.quit_agent(again)                                       # retire-peer allows it now
        self.assertEqual(self.daemon.params("thread/archive"), [{"threadId": LIVE_CODEX}] * 2)
        events = [line["event"] for line in map(json.loads,
                                                (self.m.dir / "spawned.jsonl").read_text().splitlines())]
        self.assertEqual(events, ["spawned", "retired", "resumed", "retired"])

    def test_a_stopped_spawned_claude_session_is_quit_by_recording_it(self):
        """A spawned Claude session that finished on its own keeps its place in the tree. Quit then only
        records the retirement: there is nothing left to stop, so no `claude stop` runs."""
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "claude", "name": "parked one", "id": DONE_CLAUDE[:8],
                                     "cwd": "/w", "access": "read-only", "spawned_by": "codex/x"})
        done = self.agent(f"claude:{DONE_CLAUDE}", include_quit=False)
        self.assertEqual((done.quit, done.parked), (True, False))
        self.assertEqual(menu_detail.buttons(done), ["Resume", "Quit agent"])
        self.assertIn("already stopped", menu_detail.confirm_text(done))
        menu_actions.quit_agent(done)
        self.assertNotIn(f"stop {DONE_CLAUDE[:8]}", self.m.claude_calls())
        record = json.loads((self.m.dir / "spawned.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["event"], record.get("already_stopped")), ("retired", True))
        again = self.agent(f"claude:{DONE_CLAUDE}")
        self.assertEqual((again.retired, menu_detail.buttons(again)), (True, ["Resume"]))
        with self.assertRaises(menu_actions.Refused):
            menu_actions.quit_agent(again)                                   # a second Quit has nothing to record

    def test_an_archived_spawned_codex_thread_is_resumed_before_it_is_quit(self):
        """retire-peer cannot tell an archived thread from a live one, so an archived thread nobody retired
        offers Resume only. Resume and then Quit retires it the ordinary way."""
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "archived thread",
                                     "id": ARCHIVED_CODEX, "access": "read-only", "spawned_by": "claude/x"})
        archived = self.agent(f"codex:{ARCHIVED_CODEX}")
        self.assertEqual((archived.quit, archived.spawned, archived.retired), (True, True, False))
        self.assertEqual(menu_detail.buttons(archived), ["Resume"])

    def test_a_quit_agent_takes_no_instruction(self):
        archived = self.agent(f"codex:{ARCHIVED_CODEX}")
        self.assertFalse(menu_detail.can_instruct(archived))                 # a Codex thread with no pane otherwise could
        self.assertEqual(menu_actions.perform(archived, "Send", "hello")[:2], ("not done: that is no longer offered for this agent", False))

    def test_the_quit_group_is_empty_until_the_owner_asks_for_it(self):
        """Without `a`, a parked agent can still reach the snapshot through its spawn record, for example
        one with a retirement time in the future, which counts as a bad record and so as long retired."""
        self.m.log("spawned.jsonl",
                   {"event": "spawned", "kind": "claude", "name": "parked one", "id": DONE_CLAUDE[:8], "cwd": "/w",
                    "access": "read-only", "spawned_by": "codex/x"},
                   {"event": "retired", "kind": "claude", "id": DONE_CLAUDE[:8], "action": "stop",
                    "time_utc": "2999-01-01T00:00:00Z"})
        self.assertEqual(self.snap(include_quit=False).quit_agents, [])
        self.assertIn(f"claude:{DONE_CLAUDE}", [a.key for a in self.snap(include_quit=True).quit_agents])

    # ---------- findings of the review of the merge (2026-09-17)

    def spawned(self, kind, name, agent_id, **extra):
        self.m.log("spawned.jsonl", dict({"event": "spawned", "kind": kind, "name": name, "id": agent_id, "cwd": "/w",
                                          "access": "read-only", "spawned_by": "claude/x"}, **extra))

    def test_a_resume_whose_window_fails_still_leaves_a_manageable_agent(self):
        """The unarchive is what brings the thread back, so the resume is recorded right after it. If
        the window then fails, the thread is live, keeps its buttons and its row, and can be quit again."""
        self.spawned("codex", "idle thread", LIVE_CODEX, cwd=self.m.dir.as_posix())
        key = f"codex:{LIVE_CODEX}"
        menu_actions.quit_agent(self.agent(key))
        with mock.patch.dict(os.environ, {"STUB_TMUX_FAIL": "1"}), self.assertRaises(menu_actions.Refused):
            menu_actions.resume(self.agent(key))
        events = [json.loads(line)["event"] for line in (self.m.dir / "spawned.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["spawned", "retired", "resumed"])
        back = self.agent(key)
        self.assertEqual((back.quit, back.retired), (False, False))
        self.assertIn("Quit agent", menu_detail.buttons(back))
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=11)
        self.assertIn(key, agent_state.collect(now=later).by_key)
        menu_actions.quit_agent(back)
        self.assertEqual(len(self.daemon.params("thread/archive")), 2)

    def test_an_agent_brought_back_by_hand_counts_as_running(self):
        """The README tells the owner that `claude attach` undoes a retire. That leaves no record, so a
        running session must win over the record: buttons, a row that stays, and Quit working again."""
        self.spawned("claude", "busy one", LIVE_CLAUDE[:8])
        self.m.log("spawned.jsonl", {"event": "retired", "kind": "claude", "id": LIVE_CLAUDE[:8], "action": "stop",
                                     "time_utc": "2026-01-01T00:00:00Z"})
        agent = self.agent(f"claude:{LIVE_CLAUDE}", include_quit=False)
        self.assertEqual((agent.quit, agent.retired), (False, False))
        self.assertIn("Quit agent", menu_detail.buttons(agent))
        menu_actions.quit_agent(agent)                                       # retire-peer sees it running again
        self.assertIn(f"stop {LIVE_CLAUDE[:8]}", self.m.claude_calls())

    def test_an_agent_resumed_and_then_finished_is_not_retired(self):
        """Only the latest step counts, for the menu and for retire-peer alike. A spawned session that was
        retired, resumed, and then finished on its own is stopped but not retired: it keeps its place,
        and Quit records the retirement again."""
        self.spawned("claude", "parked one", DONE_CLAUDE[:8])
        self.m.log("spawned.jsonl",
                   {"event": "retired", "kind": "claude", "id": DONE_CLAUDE[:8], "action": "stop",
                    "time_utc": spawn_stamp(3)},
                   {"event": "resumed", "kind": "claude", "id": DONE_CLAUDE[:8], "resumed_by": "owner/agent-menu",
                    "time_utc": spawn_stamp(2)})
        agent = self.agent(f"claude:{DONE_CLAUDE}", include_quit=False)
        self.assertEqual((agent.quit, agent.retired, agent.parked), (True, False, False))
        self.assertEqual(menu_detail.buttons(agent), ["Resume", "Quit agent"])
        note, succeeded, _ = menu_actions.perform(agent, "Quit agent")
        self.assertTrue(succeeded, note)
        record = json.loads((self.m.dir / "spawned.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["event"], record.get("already_stopped")), ("retired", True))

    def test_a_record_only_quit_never_stops_a_session_that_came_back(self):
        """The confirm promised a record only. If the session was attached again since the popup read it,
        retire-peer refuses instead of stopping it."""
        self.spawned("claude", "parked one", DONE_CLAUDE[:8])
        agent = self.agent(f"claude:{DONE_CLAUDE}", include_quit=False)
        self.assertIn("record it as retired", menu_detail.confirm_text(agent))
        self.m.claude_finished.clear()
        self.m.claude_session(DONE_CLAUDE, "parked one", 600)                # attached in another window meanwhile
        self.m.write()
        note, succeeded, _ = menu_actions.perform(agent, "Quit agent")
        self.assertFalse(succeeded, note)
        self.assertIn("running again", note)
        self.assertFalse(any(c.startswith("stop") for c in self.m.claude_calls()))

    def test_no_quit_while_any_pane_could_be_showing_the_thread(self):
        """A thread gets a pane only when one pane fits it for certain. When two panes could be showing
        it, it gets none, and Quit would archive a thread the owner has open."""
        self.m.pane(4, 400, "codex", "idle thread | proj")
        self.m.pane(5, 401, "codex", "idle thread | proj-copy")
        self.m.write()
        agent = self.agent(f"codex:{LIVE_CODEX}")
        self.assertEqual((agent.pane_id, agent.maybe_in_pane), ("", True))
        self.assertNotIn("Quit agent", menu_detail.buttons(agent))
        self.assertIn("pane may be showing it", menu_detail.quit_note(agent))
        with self.assertRaises(menu_actions.Refused):
            menu_actions.quit_agent(agent)
        self.assertEqual(self.daemon.params("thread/archive"), [])

    def test_any_choice_menu_turns_the_instruction_line_off(self):
        """Enter picks the highlighted option in any choice menu, not only Yes/No approvals."""
        question = ("Which database should we use?\n\n❯ 1. PostgreSQL\n     Robust\n  2. SQLite\n"
                    "  3. Type something.\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n")
        for screen in (question, "❯ 1. PostgreSQL\n  2. SQLite\n", "pick one\nEnter to select · Esc to cancel\n"):
            with self.subTest(screen=screen):
                self.assertTrue(agent_state.waits_on_owner("", screen))
        for screen in ("Plan:\n1. Read the tests\n2. Fix the bug\n", "❯ run the tests\n"):
            with self.subTest(screen=screen):
                self.assertFalse(agent_state.waits_on_owner("", screen))   # a list in the output, or typed text
        self.m.claude_session("cccccccc-0000-4000-8000-000000000003", "at terminal", 900, background=False)
        self.m.pane(3, 800, "claude", "✳ at terminal", screen=question)
        self.m.process(900, 800)
        self.m.write()
        agent = self.agent("claude:cccccccc-0000-4000-8000-000000000003")
        self.assertEqual((agent.pane_id != "", agent.needs_owner > 0), (True, True))
        self.assertNotIn("Send", menu_detail.buttons(agent))
        self.assertEqual(menu_actions.perform(agent, "Send", "wait, explain first")[1], False)
        self.assertFalse(any(c.startswith("send-keys") for c in self.m.tmux_calls()))

    def test_a_failed_lookup_of_stopped_sessions_is_reported(self):
        """A stopped child dropped because `--all` could not be read must leave a line saying so, and
        the live rows stay. An action that re-checks a session still refuses on the same failure."""
        claude_src._finished.update(missing=frozenset(), at=float("-inf"), sessions=[])
        self.spawned("claude", "parked one", DONE_CLAUDE[:8])
        (self.m.dir / "claude-all.json").write_text("not json")
        snap = self.snap(include_quit=False)
        self.assertIn("claude: finished sessions unreadable", snap.errors)
        self.assertIn(f"claude:{LIVE_CLAUDE}", snap.by_key)
        snap = self.snap(include_quit=True)
        self.assertIn("claude: quit sessions unreadable", snap.errors)
        self.assertIn(f"claude:{LIVE_CLAUDE}", snap.by_key)
        parked = agent_state.Agent(key=f"claude:{DONE_CLAUDE}", kind="claude", name="parked one",
                                   session_id=DONE_CLAUDE, short_id=DONE_CLAUDE[:8], quit=True)
        with self.assertRaises(menu_actions.Refused):
            menu_actions.resume(parked)

    def test_a_live_agent_whose_parent_was_quit_stays_in_the_tree(self):
        self.m.log("spawned.jsonl", {"event": "spawned", "kind": "codex", "name": "idle thread", "id": LIVE_CODEX,
                                     "access": "read-only", "spawned_by": f"claude/parked one [{DONE_CLAUDE[:8]}]"})
        snap = self.snap()
        self.assertIn(f"codex:{LIVE_CODEX}", [a.key for a in snap.roots])  # a root, not hidden under a quit row

    def test_resume_is_refused_for_live_agents_and_changed_ids(self):
        with self.assertRaises(menu_actions.Refused):
            menu_actions.resume(self.agent(f"codex:{LIVE_CODEX}"))
        parked = self.agent(f"claude:{DONE_CLAUDE}")
        self.m.claude_finished[0]["name"] = "renamed"
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.resume(parked)
        self.m.claude_finished[0]["name"] = "parked one"                     # running again since it was read
        self.m.claude_session(DONE_CLAUDE, "parked one", 600)
        self.m.write()
        with self.assertRaises(menu_actions.Refused):
            menu_actions.resume(parked)
        self.assertEqual(self.acted(), [])

    def test_a_folder_with_a_hash_or_that_does_not_exist_is_not_passed_to_tmux(self):
        from sources import tmux_src
        for folder in ("/no/such/folder", str(self.m.dir / "a#b")):
            with self.subTest(folder=folder):
                tmux_src.new_window("x", "true", cwd=folder)
                self.assertNotIn(" -c ", self.acted()[-1])

    def test_look_only_refuses_quit_but_allows_resume(self):
        with mock.patch.dict(os.environ, {"AGENT_MENU_LOOK_ONLY": "1"}):
            self.assertEqual(menu_actions.perform(self.agent(f"codex:{LIVE_CODEX}"), "Quit agent")[:2],
                             ("not done: look-only mode", False))
            self.assertEqual(menu_actions.perform(self.agent(f"codex:{ARCHIVED_CODEX}"), "Resume")[1], True)


if __name__ == "__main__":
    unittest.main()
