"""Hardening from the product review before the repository went public: nothing a peer can write widens
what an agent may do, the logs stay private, the menu's own `claude` call runs no hooks, and text a peer
chose never reaches the terminal raw. Every test uses stubs, fakes and temp folders."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from menu_fixtures import LIB, Machine  # noqa: E402
from test_peers import BIN, QUEUEING_STUB, FakeCodexDaemon  # noqa: E402
from test_send_to_claude import Fixture as ClaudeSocketFixture  # noqa: E402

sys.path.insert(0, str(LIB))
import agent_state  # noqa: E402
import menu_actions  # noqa: E402
import spawn_log  # noqa: E402
from sources import limits_src  # noqa: E402

THREAD = "0f0f0f0f-0000-7000-8000-000000000001"
MSG = "11111111-2222-4333-8444-555555555555"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class UsageCallTest(unittest.TestCase):
    """The menu reads Claude's limits every 15 minutes. That call must run no hooks, MCP servers or
    project settings, which anyone able to write its folder could plant, and must leave no transcript."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uc")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        self.folder = self.m.dir / "fresh" / "usage-calls"                 # never the repository's own log/
        patcher = mock.patch.dict(os.environ, dict(self.m.env, AGENT_MENU_USAGE_CWD=str(self.folder)), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_usage_call_is_sealed_off(self):
        limits_src.claude_limits()
        (call,) = [c for c in self.m.claude_calls() if c.startswith("-p")]
        for flag in ("--safe-mode", "--no-session-persistence", "--setting-sources user"):
            self.assertIn(flag, call)
        self.assertTrue(call.endswith("/usage"), call)
        self.assertEqual(mode(self.folder), 0o700)


class WakeHardeningTest(unittest.TestCase):
    """The spawn record is a plain file a peer may be able to append to. A wake must never take wider
    settings, or wake a thread spawn-peer did not create, because of a line in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wh")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.work = self.home / "work"
        self.work.mkdir()
        self.daemon = FakeCodexDaemon(str(self.tmp / "d.sock"))
        self.daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": str(self.work)}
        self.daemon.status[THREAD] = "notLoaded"
        self.stub = self.tmp / "codex-stub"
        self.stub.write_text(QUEUEING_STUB)
        self.stub.chmod(0o755)
        self.record_path = self.tmp / "spawned.jsonl"

    def record(self, *events):
        with open(self.record_path, "a") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        os.chmod(self.record_path, 0o600)

    def spawned(self, **over):
        event = {"event": "spawned", "kind": "codex", "name": "peer", "id": THREAD, "cwd": str(self.work),
                 "access": "read-only", "approvals": "never", "spawned_by": "claude/x"}
        event.update(over)
        return event

    def send(self):
        env = dict(os.environ, HOME=str(self.home), CODEX_BIN=str(self.stub), STUB_DIR=str(self.tmp),
                   CODEX_APP_SERVER_SOCK=str(self.tmp / "d.sock"), AGENT_COMMS_LOG=str(self.tmp / "m.jsonl"),
                   AGENT_COMMS_SPAWN_LOG=str(self.record_path), SEND_TO_CODEX_WAIT_SECONDS="1")
        return subprocess.run([str(BIN / "send-to-codex"), THREAD, "claude/t", "Follow-up"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_later_line_can_never_widen_the_first(self):
        self.record(self.spawned(), self.spawned(access="write", approvals="auto-review", approval="forged"))
        self.send()
        (params,) = self.daemon.params("thread/resume")
        self.assertEqual((params["sandbox"], params["approvalPolicy"]), ("read-only", "never"))
        self.assertNotIn("approvalsReviewer", params)

    def test_no_wake_when_the_record_names_another_folder_than_the_daemon(self):
        other = self.home / "elsewhere"
        other.mkdir()
        self.record(self.spawned(cwd=str(other)))
        self.send()
        self.assertNotIn("thread/resume", self.daemon.methods())

    def test_no_wake_in_a_protected_folder(self):
        self.daemon.threads[THREAD]["cwd"] = str(self.home / ".codex")
        self.record(self.spawned(cwd=str(self.home / ".codex")))
        self.send()
        self.assertNotIn("thread/resume", self.daemon.methods())

    def test_no_wake_for_a_thread_the_daemon_says_a_person_started(self):
        self.daemon.threads[THREAD]["threadSource"] = "user"
        self.record(self.spawned())
        self.send()
        self.assertNotIn("thread/resume", self.daemon.methods())

    def test_no_wake_when_others_can_write_the_record(self):
        self.record(self.spawned())
        os.chmod(self.record_path, 0o602)
        self.send()
        self.assertNotIn("thread/resume", self.daemon.methods())

    def test_a_group_writable_record_is_trusted_only_in_a_personal_group(self):
        self.record(self.spawned())
        os.chmod(self.record_path, 0o620)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_LOG": str(self.record_path)}):
            with mock.patch.object(spawn_log, "_personal_group", return_value=False):
                self.assertFalse(spawn_log.trusted())
            with mock.patch.object(spawn_log, "_personal_group", return_value=True):
                self.assertTrue(spawn_log.trusted())

    def test_the_checks_still_let_a_real_spawned_agent_wake(self):
        self.record(self.spawned())
        self.assertEqual(self.send().returncode, 0)
        self.assertEqual(len(self.daemon.params("thread/resume")), 1)


class PrivateLogsTest(unittest.TestCase):
    """Logs hold message summaries and your own instructions. They are created 0600 in 0700 folders,
    and a log left readable by others is tightened the next time it is written."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pl")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_send_to_codex_log_is_private_and_an_open_one_is_tightened(self):
        daemon = FakeCodexDaemon(str(self.tmp / "d.sock"))
        daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": "/w"}
        daemon.status[THREAD] = "idle"
        stub = self.tmp / "codex-stub"
        stub.write_text(QUEUEING_STUB)
        stub.chmod(0o755)
        fresh = self.tmp / "new-folder" / "m.jsonl"
        old = self.tmp / "old.jsonl"
        old.write_text("")
        os.chmod(old, 0o664)
        for log in (fresh, old):
            env = dict(os.environ, CODEX_BIN=str(stub), STUB_DIR=str(self.tmp), CODEX_APP_SERVER_SOCK=str(self.tmp / "d.sock"),
                       AGENT_COMMS_LOG=str(log), AGENT_COMMS_SPAWN_LOG=str(self.tmp / "s.jsonl"),
                       SEND_TO_CODEX_WAIT_SECONDS="1")
            proc = subprocess.run([str(BIN / "send-to-codex"), THREAD, "claude/t", "Hello"], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(mode(log), 0o600, log)
        self.assertEqual(mode(fresh.parent), 0o700)

    def test_log_receipt_log_is_private(self):
        log = self.tmp / "m.jsonl"
        log.write_text(json.dumps({"channel": "codex queue", "msg_id": MSG, "result": "queued"}) + "\n")
        os.chmod(log, 0o664)
        proc = subprocess.run([str(BIN / "log-receipt"), MSG, "codex/x", "cited it"], capture_output=True,
                              text=True, timeout=30, env=dict(os.environ, AGENT_COMMS_LOG=str(log)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(mode(log), 0o600)

    def test_spawn_record_and_menu_log_are_private(self):
        with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_LOG": str(self.tmp / "sub" / "spawned.jsonl"),
                                          "AGENT_MENU_ACTIONS_LOG": str(self.tmp / "sub2" / "menu.jsonl")}):
            spawn_log.append({"event": "spawned", "id": "x"})
            menu_actions._log(agent_state.Agent(key="k", kind="codex", name="n", session_id="s"), "test", "ok")
        for path in (self.tmp / "sub" / "spawned.jsonl", self.tmp / "sub2" / "menu.jsonl"):
            self.assertEqual(mode(path), 0o600, path)
            self.assertEqual(mode(path.parent), 0o700, path)

    def test_send_to_claude_log_is_private(self):
        fx = ClaudeSocketFixture(self.tmp)
        fx.write_registry()
        fx.write_key()
        fx.serve()
        proc = fx.run(fx.session_id, "codex/t", "Hello")
        fx.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(mode(fx.log), 0o600)


class UnwritableLogTest(unittest.TestCase):
    """A log that cannot be appended to must never be reported as logged. In bash, `if ! { ...; } 9>>f`
    skips the `!` when the redirection itself fails, which made both tools print success."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ul")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "m.jsonl"

    def codex(self, stub_after=""):
        daemon = FakeCodexDaemon(str(self.tmp / "d.sock"))
        daemon.threads[THREAD] = {"id": THREAD, "name": "peer", "cwd": "/w"}
        daemon.status[THREAD] = "idle"
        real = self.tmp / "real-stub"
        real.write_text(QUEUEING_STUB)
        real.chmod(0o755)
        stub = self.tmp / "codex-stub"
        stub.write_text(f'#!/usr/bin/env bash\n"{real}" "$@"; rc=$?\n{stub_after}\nexit $rc\n')
        stub.chmod(0o755)
        env = dict(os.environ, CODEX_BIN=str(stub), STUB_DIR=str(self.tmp), CODEX_APP_SERVER_SOCK=str(self.tmp / "d.sock"),
                   AGENT_COMMS_LOG=str(self.log), AGENT_COMMS_SPAWN_LOG=str(self.tmp / "s.jsonl"),
                   SEND_TO_CODEX_WAIT_SECONDS="1")
        proc = subprocess.run([str(BIN / "send-to-codex"), THREAD, "claude/t", "Hello"], env=env,
                              capture_output=True, text=True, timeout=60)
        return daemon, proc

    def test_a_log_unwritable_from_the_start_means_nothing_is_sent(self):
        self.log.write_text("")
        os.chmod(self.log, 0o400)
        daemon, proc = self.codex()
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("thread/queue/add", daemon.methods())
        self.assertIn("nothing sent", proc.stderr)

    def test_a_log_that_fails_after_the_send_says_unlogged_and_prints_the_line(self):
        daemon, proc = self.codex(stub_after=f'chmod 400 "{self.log}"')
        self.assertEqual(daemon.taken, ["q-1"])                  # it was sent
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Logged to", proc.stdout)
        self.assertIn('"msg_id"', proc.stderr)                  # the unlogged line, so it is not lost

    def test_log_receipt_says_unlogged(self):
        self.log.write_text(json.dumps({"channel": "codex queue", "msg_id": MSG, "result": "queued"}) + "\n")
        os.chmod(self.log, 0o400)
        proc = subprocess.run([str(BIN / "log-receipt"), MSG, "codex/x", "cited it"], capture_output=True,
                              text=True, timeout=30, env=dict(os.environ, AGENT_COMMS_LOG=str(self.log)))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("Receipt logged", proc.stdout)


class ResumeSettingsTest(unittest.TestCase):
    """Resume opens `codex resume` in a window. For an agent spawn-peer created it passes the spawn's own
    settings, or your config.toml defaults would apply, and those can be wider."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rs")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.daemon = FakeCodexDaemon(self.m.sock)
        self.daemon.threads[THREAD] = {"id": THREAD, "name": "kid", "cwd": "/w/kid"}
        self.daemon.archived.add(THREAD)

    def resume_call(self, **record):
        if record:
            self.m.log("spawned.jsonl", dict({"event": "spawned", "kind": "codex", "name": "kid", "id": THREAD,
                                              "cwd": "/w/kid", "access": "read-only"}, **record),
                       {"event": "retired", "kind": "codex", "id": THREAD, "action": "archive"})
        agent = agent_state.Agent(key=f"codex:{THREAD}", kind="codex", name="kid", session_id=THREAD,
                                  cwd="/w/kid", quit=True, spawned=bool(record), retired=bool(record))
        menu_actions.resume(agent)
        (call,) = [c for c in self.m.tmux_calls() if c.startswith("new-window")]
        return call

    def test_a_spawned_agent_resumes_with_its_recorded_settings(self):
        call = self.resume_call(approvals="auto-review")
        self.assertIn(f"""resume -s read-only -a on-request -c 'approvals_reviewer="auto_review"' -C /w/kid {THREAD}""", call)

    def test_unknown_approvals_resume_with_the_strictest(self):
        self.assertIn("-s read-only -a never -C /w/kid", self.resume_call(approvals=None))

    def test_a_thread_you_started_yourself_resumes_as_before(self):
        call = self.resume_call()
        self.assertNotIn(" -s ", call)
        self.assertTrue(call.endswith(f"resume {THREAD}"), call)


class RawTextTest(unittest.TestCase):
    """Names, folders and answers come from other agents. Escape sequences in them never reach a terminal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rt")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_send_to_claude_list_and_dry_run_print_no_escape_sequences(self):
        fx = ClaudeSocketFixture(self.tmp)
        fx.write_registry(name="evil\x1b]52;c;aGk=\x07\x1b[2J", cwd="/tmp/\x1b[31mred")
        for args in (["--list"], ["--dry-run", fx.session_id, "codex/t", "hi"]):
            proc = fx.run(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("\x1b", proc.stdout, args)
            self.assertNotIn("\x07", proc.stdout, args)

    def test_codex_reply_keeps_line_breaks_but_no_escape_sequences(self):
        daemon = FakeCodexDaemon(str(self.tmp / "d.sock"))
        daemon.threads[THREAD] = {"id": THREAD, "name": "n\x1b[2J", "cwd": "/w", "turns": [
            {"status": "completed", "items": [{"type": "agentMessage", "text": "line one\n\x1b]52;c;aGk=\x07line two"}]}]}
        proc = subprocess.run([sys.executable, str(BIN / "codex-reply"), THREAD], capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, CODEX_APP_SERVER_SOCK=str(self.tmp / "d.sock")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("\x1b", proc.stdout)
        self.assertIn("line one\n", proc.stdout)


if __name__ == "__main__":
    unittest.main()
