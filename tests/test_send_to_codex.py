"""Black-box tests for bin/send-to-codex. A stub stands in for `codex` and a fake daemon
for the Codex app-server, so nothing is queued to a real session."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_peers import FakeCodexDaemon  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin") / "send-to-codex"
LIB = Path(os.environ.get("AGENT_COMMS_LIB") or ROOT / "lib")
THREAD = "0b0b0b0b-0000-7000-8000-000000000001"

# Plays `codex queue` the way the real one works: it adds the message to the daemon's queue for
# the thread, and a turn starts only if the daemon has that thread loaded and idle.
QUEUEING_STUB = f"""#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_DIR/argv.txt"
exec python3 - "$@" <<'PY'
import sys
sys.path.insert(0, {str(LIB)!r})
from codex_ws import CodexWS
a = sys.argv[1:]
thread = a[a.index("--thread") + 1]
ws = CodexWS(experimental=True)
item = ws.rpc("thread/queue/add", {{"threadId": thread, "input": [{{"type": "text", "text": a[a.index("--message") + 1]}}]}})["id"]
ws.close()
print(f"Queued message {{item}} for thread {{thread}}.")
PY
"""


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


def run_tool(tmp, stub, *args, stdin=None, **env_extra):
    """Every run points at a socket and a spawn record inside `tmp`, never the real daemon or record."""
    env = dict(os.environ, CODEX_BIN=str(stub), AGENT_COMMS_LOG=str(Path(tmp) / "messages.jsonl"),
               CODEX_APP_SERVER_SOCK=str(Path(tmp) / "d.sock"), STUB_DIR=str(tmp),
               AGENT_COMMS_SPAWN_LOG=str(Path(tmp) / "spawned.jsonl"), SEND_TO_CODEX_WAIT_SECONDS="1")
    env.update(env_extra)
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
        self.daemon = FakeCodexDaemon(str(Path(self.tmp) / "d.sock"))
        self.daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": "/w"}
        self.daemon.status[THREAD] = "idle"

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
        self.assertEqual(self.daemon.calls, [])  # nor does it ask the daemon anything

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



class DeliveryTest(unittest.TestCase):
    """`codex queue` only stores a message. The daemon runs it only on a loaded thread, and it unloads
    a thread about a minute after the thread goes idle with nobody attached, which is every spawned
    agent. These tests pin that the tool wakes a spawned agent with its recorded settings, and says
    so plainly when a message will wait."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dlv")
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.daemon = FakeCodexDaemon(str(Path(self.tmp) / "d.sock"))
        self.daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": "/w"}
        self.stub = Path(self.tmp) / "codex-stub"
        self.stub.write_text(QUEUEING_STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)

    def record(self, *events):
        with open(Path(self.tmp) / "spawned.jsonl", "a") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")

    def spawned(self, access="read-only", approvals="auto-review"):
        return {"event": "spawned", "kind": "codex", "name": "peer", "id": THREAD, "cwd": "/w",
                "access": access, "approvals": approvals, "spawned_by": "claude/x"}

    def send(self, **env_extra):
        return run_tool(self.tmp, self.stub, THREAD, "claude/test", "Follow-up request", **env_extra)

    def test_an_idle_loaded_thread_takes_the_message_and_is_not_resumed(self):
        self.daemon.status[THREAD] = "idle"
        proc = self.send()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.daemon.taken, ["q-1"])
        self.assertNotIn("thread/resume", self.daemon.methods())
        (line,) = read_log(self.tmp)
        self.assertEqual((line["result"], line["thread_status"], line["woke"], line["delivery"]),
                         ("queued", "idle", False, "started"))

    def test_an_unloaded_spawned_agent_is_woken_with_its_recorded_settings_before_the_queue(self):
        # The bug: before the fix this message stayed queued and no turn ever started.
        self.record(self.spawned())
        self.daemon.status[THREAD] = "notLoaded"
        proc = self.send()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.daemon.taken, ["q-1"])
        self.assertEqual(self.daemon.params("thread/resume"),
                         [{"threadId": THREAD, "cwd": "/w", "sandbox": "read-only", "approvalPolicy": "on-request",
                           "approvalsReviewer": "auto_review", "excludeTurns": True}])
        methods = self.daemon.methods()
        self.assertLess(methods.index("thread/resume"), methods.index("thread/queue/add"))
        (line,) = read_log(self.tmp)
        self.assertEqual((line["thread_status"], line["woke"], line["delivery"]), ("notLoaded", True, "started"))

    def test_a_wake_never_widens_what_the_owner_chose(self):
        self.record(dict(self.spawned(access="write", approvals="never"), approval="owner, chat"))
        self.daemon.status[THREAD] = "notLoaded"
        self.assertEqual(self.send().returncode, 0)
        (params,) = self.daemon.params("thread/resume")
        self.assertEqual((params["sandbox"], params["approvalPolicy"]), ("workspace-write", "never"))
        self.assertNotIn("approvalsReviewer", params)

    def test_a_record_without_known_settings_is_not_woken(self):
        record = self.spawned()
        del record["approvals"]
        self.record(record)
        self.daemon.status[THREAD] = "notLoaded"
        proc = self.send()
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertNotIn("thread/resume", self.daemon.methods())

    def test_an_unloaded_thread_with_no_spawn_record_is_not_woken_and_exits_5(self):
        self.daemon.status[THREAD] = "notLoaded"
        proc = self.send()
        self.assertEqual(proc.returncode, 5)
        self.assertNotIn("thread/resume", self.daemon.methods())
        self.assertEqual(self.daemon.queued_ids(), {THREAD: ["q-1"]})  # queued, and honestly reported as waiting
        self.assertIn("not loaded", proc.stderr)
        (line,) = read_log(self.tmp)
        self.assertEqual((line["result"], line["woke"], line["delivery"]), ("queued", False, "not-loaded"))

    def test_a_retired_agent_is_not_woken_until_it_is_resumed(self):
        self.record(self.spawned(), {"event": "retired", "kind": "codex", "id": THREAD, "action": "archive"})
        self.daemon.status[THREAD] = "notLoaded"
        self.assertEqual(self.send().returncode, 5)
        self.assertNotIn("thread/resume", self.daemon.methods())
        self.record({"event": "resumed", "kind": "codex", "id": THREAD})
        self.assertEqual(self.send().returncode, 0)
        self.assertEqual(len(self.daemon.params("thread/resume")), 1)

    def test_a_busy_thread_holds_the_message_behind_its_turn(self):
        self.daemon.status[THREAD] = "active"
        proc = self.send()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("thread/resume", self.daemon.methods())
        self.assertEqual(read_log(self.tmp)[0]["delivery"], "waiting")

    def test_a_loaded_thread_that_never_takes_the_message_exits_5(self):
        self.daemon.status[THREAD] = "idle"
        self.daemon.stall = True
        proc = self.send()
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(read_log(self.tmp)[0]["delivery"], "stuck")

    def test_an_agent_unloaded_between_the_check_and_the_queue_is_woken_after(self):
        self.record(self.spawned())
        self.daemon.status[THREAD] = "idle"
        real_answer = self.daemon._answer

        def unload_first(method, params, experimental=False):
            if method == "thread/queue/add":
                self.daemon.status[THREAD] = "notLoaded"  # the daemon's idle timer fired just now
            return real_answer(method, params, experimental)
        self.daemon._answer = unload_first
        proc = self.send()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.daemon.taken, ["q-1"])
        (line,) = read_log(self.tmp)
        self.assertEqual((line["woke"], line["delivery"]), (True, "started"))

    def test_the_message_is_found_by_its_msg_id_when_the_queue_lists_another_id(self):
        # Codex does not document that the id `codex queue` prints is the id the queue lists. If they
        # ever differ, an id-only check would report every waiting message as taken.
        self.daemon.status[THREAD] = "notLoaded"
        self.daemon.list_other_ids = True
        proc = self.send()
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertEqual(read_log(self.tmp)[0]["delivery"], "not-loaded")

    def test_no_daemon_means_unconfirmed_never_success(self):
        stub, _ = make_stub(self.tmp, 0, f"Queued message q-7 for thread {THREAD}.")
        proc = run_tool(self.tmp, stub, THREAD, "claude/test", "Summary", CODEX_APP_SERVER_SOCK=str(Path(self.tmp) / "none.sock"))
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(read_log(self.tmp)[0]["delivery"], "unknown")


if __name__ == "__main__":
    unittest.main()
