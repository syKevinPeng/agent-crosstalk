"""Black-box tests for bin/spawn-peer and bin/retire-peer. A local thread plays the
Codex daemon and a script plays `claude`, so no real agent is created or removed."""
import base64
import hashlib
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin")
SPAWN = BIN / "spawn-peer"
RETIRE = BIN / "retire-peer"
LIB = Path(os.environ.get("AGENT_COMMS_LIB") or ROOT / "lib")
THREAD_ID = "0c0c0c0c-0000-7000-8000-00000000abcd"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

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


class FakeCodexDaemon:
    """Accepts WebSocket connections on a Unix socket and keeps threads in memory."""

    def __init__(self, path, existing_names=(), fail=()):
        self.calls = []
        self.archived = set()
        self.fail = set(fail)
        # The queue, as the real daemon runs it: a queued message starts a turn only on a thread that
        # is loaded and idle. `status` holds notLoaded / idle / active for the threads a test sets.
        self.status, self.queue, self.taken = {}, {}, []
        self.stall = False  # when True, even an idle loaded thread leaves its queue alone
        self.list_other_ids = False  # when True, the queue lists an id other than the one `queue/add` gave
        self.list_shape = "data"     # the key the queue listing uses; anything else plays a changed daemon
        self.vanish = False          # when True, a queued message disappears without any turn taking it
        self._queued = 0
        self.threads = {f"old-{i}": {"id": f"old-{i}", "name": n, "cwd": "/x"}
                        for i, n in enumerate(existing_names)}
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _take(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _drain(self, tid):
        if not self.stall and self.status.get(tid) == "idle" and self.queue.get(tid):
            self.taken += [item["id"] for item in self.queue.pop(tid)]
            self.status[tid] = "active"

    def queued_ids(self):
        return {tid: [item["id"] for item in items] for tid, items in self.queue.items() if items}

    def _answer(self, method, params, experimental=False):
        if method.startswith("thread/queue/") and not experimental:
            raise LookupError(f"{method} requires experimentalApi capability")
        if method.startswith("thread/queue/") and params.get("threadId") in self.archived:
            raise LookupError(f"session {params['threadId']} is archived. Run `codex unarchive` to unarchive it first.")
        if method == "thread/queue/add":
            self._queued += 1
            item = {"id": f"q-{self._queued}", "clientUserMessageId": f"c-{self._queued}",
                    "input": params.get("input") or []}
            if not self.vanish:
                self.queue.setdefault(params["threadId"], []).append(item)
                self._drain(params["threadId"])
            return {"id": item["id"]}
        if method == "thread/queue/list":
            items = self.queue.get(params["threadId"], [])
            if self.list_other_ids:
                items = [dict(i, id="other-" + i["id"], clientUserMessageId="other") for i in items]
            return {self.list_shape: items, "nextCursor": None}
        if method == "thread/resume":
            tid = params["threadId"]
            if self.status.get(tid, "notLoaded") == "notLoaded":
                self.status[tid] = "idle"
            self._drain(tid)
            return {"thread": self.threads.get(tid), "sandbox": params.get("sandbox"),
                    "approvalPolicy": params.get("approvalPolicy")}
        if method == "account/rateLimits/read":
            return getattr(self, "account", {})
        if method == "thread/list":
            # Like the real daemon: archived threads are listed only when asked for, and only then.
            hidden = getattr(self, "hidden_from_list", set())
            want_archived = bool(params.get("archived"))
            return {"data": [t for t in self.threads.values()
                             if t["id"] not in hidden and (t["id"] in self.archived) == want_archived]}
        if method == "thread/archive":
            self.archived.add(params["threadId"])
        if method == "thread/unarchive":
            self.archived.discard(params["threadId"])
        if method == "thread/start":
            self.threads[THREAD_ID] = {"id": THREAD_ID, "name": None, "cwd": params.get("cwd")}
            self.status[THREAD_ID] = "idle"  # a new thread is loaded, like on the real daemon
            return {"thread": self.threads[THREAD_ID]}
        if method == "thread/name/set":
            self.threads[params["threadId"]]["name"] = params["name"]
        if method == "thread/read":
            thread = self.threads.get(params["threadId"])
            if thread is not None and params["threadId"] in self.status:
                thread = dict(thread, status={"type": self.status[params["threadId"]]})
            return {"thread": thread}
        return {}

    def _serve(self, conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(4096)
            key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n")
                   if l.lower().startswith(b"sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1(key + WS_GUID.encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            experimental = False
            while True:
                b0, b1 = self._take(conn, 2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._take(conn, 2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._take(conn, 8))[0]
                mask = self._take(conn, 4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._take(conn, n)))
                if b0 & 0x0F == 0x8:
                    return
                msg = json.loads(data)
                self.calls.append((msg.get("method"), msg.get("params")))
                if "id" not in msg:
                    continue
                if msg["method"] == "initialize":
                    experimental = bool(((msg.get("params") or {}).get("capabilities") or {}).get("experimentalApi"))
                if msg["method"] in self.fail:
                    body = {"id": msg["id"], "error": {"code": -1, "message": "refused by fake"}}
                else:
                    try:
                        body = {"id": msg["id"], "result": self._answer(msg["method"], msg.get("params") or {},
                                                                        experimental)}
                    except LookupError as exc:
                        body = {"id": msg["id"], "error": {"code": -32600, "message": str(exc)}}
                out = json.dumps(body).encode()
                head = bytes([0x81, len(out)]) if len(out) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(out))
                conn.sendall(head + out)
        except (ConnectionError, OSError, IndexError):
            pass
        finally:
            conn.close()

    def methods(self):
        return [m for m, _ in self.calls]

    def params(self, method):
        return [p for m, p in self.calls if m == method]


CLAUDE_STUB = r"""#!/usr/bin/env bash
state="$STUB_DIR/sessions.json"; [[ -f $state ]] || echo '[]' > "$state"
printf '%s\n' "$PWD" "$@" >> "$STUB_DIR/claude-argv.txt"; echo '--' >> "$STUB_DIR/claude-argv.txt"
case $1 in
  agents) [[ -n ${STUB_AGENTS_FAIL_AFTER_LAUNCH:-} && -f "$STUB_DIR/launched" ]] && exit 5
          [[ -n ${STUB_PLAIN_AGENTS_FAIL:-} && " $* " != *" --all "* ]] && exit 6
          # finished.json holds sessions that stopped: `--all` lists them, the plain listing does not
          if [[ " $* " == *" --all "* && -f "$STUB_DIR/finished.json" ]]; then
            jq -s 'add' "$state" "$STUB_DIR/finished.json"
          else cat "$state"; fi ;;
  --bg) echo "${STUB_BG_OUTPUT:-started ab12cd34}"; touch "$STUB_DIR/launched"
        [[ -n ${STUB_NEVER_LISTED:-} ]] && exit 0
        jq --arg n "$3" --arg c "${STUB_REGISTER_CWD:-$PWD}" '. + [{"id":"ab12cd34","sessionId":"ab12cd34-0000-4000-8000-000000000000","name":$n,"cwd":$c,"kind":"background"}]' "$state" > "$state.new" && mv "$state.new" "$state" ;;
  stop|rm) [[ -n ${STUB_FAIL:-} ]] && { echo "boom" >&2; exit 9; }; echo "ok" ;;
esac
"""


class PeersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pr")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        for sub in (".claude", ".codex", "projects/work"):
            (self.home / sub).mkdir(parents=True)
        self.work = self.home / "projects" / "work"
        self.sock = str(self.tmp / "d.sock")
        self.spawn_log = self.tmp / "spawned.jsonl"
        stub = self.tmp / "claude-stub"
        stub.write_text(CLAUDE_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        codex_stub = self.tmp / "codex-stub"
        codex_stub.write_text(QUEUEING_STUB.replace("argv.txt", "codex-argv.txt"))
        codex_stub.chmod(codex_stub.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_APP_SERVER_SOCK=self.sock, CLAUDE_BIN=str(stub),
                        CODEX_BIN=str(codex_stub), STUB_DIR=str(self.tmp),
                        AGENT_COMMS_SPAWN_LOG=str(self.spawn_log),
                        SPAWN_PEER_POLL_SECONDS="1",
                        AGENT_COMMS_LOG=str(self.tmp / "messages.jsonl"))

    def run_tool(self, tool, *args, **env_extra):
        return subprocess.run([sys.executable, str(tool), *args], env=dict(self.env, **env_extra),
                              capture_output=True, text=True, timeout=60)

    def spawn_codex(self, *extra):
        return self.run_tool(SPAWN, *extra, "--cwd", str(self.work), "codex", "peer-check", "claude/t")

    def spawn_claude(self, *extra, **env_extra):
        return self.run_tool(SPAWN, *extra, "--cwd", str(self.work), "claude", "peer-check", "codex/t",
                             "Please wait", **env_extra)

    def records(self):
        if not self.spawn_log.exists():
            return []
        return [json.loads(l) for l in self.spawn_log.read_text().splitlines()]

    def claude_argv(self):
        path = self.tmp / "claude-argv.txt"
        return path.read_text() if path.exists() else ""

    # --- spawn codex ---
    def test_spawn_codex_is_read_only_named_and_recorded(self):
        daemon = FakeCodexDaemon(self.sock)
        proc = self.spawn_codex()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.methods(), ["initialize", "initialized", "thread/list", "thread/start", "thread/name/set"])
        self.assertEqual(daemon.params("thread/start")[0],  # the project default: automatic reviewer
                         {"cwd": str(self.work.resolve()), "sandbox": "read-only",
                          "approvalPolicy": "on-request", "approvalsReviewer": "auto_review"})
        self.assertEqual(daemon.params("thread/name/set")[0], {"threadId": THREAD_ID, "name": "peer-check"})
        (rec,) = self.records()
        self.assertEqual((rec["event"], rec["kind"], rec["id"], rec["access"], rec["spawned_by"], rec["approval"]),
                         ("spawned", "codex", THREAD_ID, "read-only", "claude/t", None))
        self.assertEqual(rec["approvals"], "auto-review")
        self.assertFalse((self.tmp / "codex-argv.txt").exists())  # no first message, so nothing queued

    def test_write_needs_recorded_approval_and_never_means_full_access(self):
        daemon = FakeCodexDaemon(self.sock)
        self.assertEqual(self.spawn_codex("--write").returncode, 2)
        self.assertEqual(self.spawn_codex("--write", "--approval", "   ").returncode, 2)
        self.assertEqual(daemon.calls, [])
        proc = self.spawn_codex("--write", "--approval", "owner, chat 2026-01-01")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/start")[0]["sandbox"], "workspace-write")
        self.assertEqual((self.records()[0]["access"], self.records()[0]["approval"]),
                         ("write", "owner, chat 2026-01-01"))

    def test_approvals_never_is_an_option_codex_only_and_recorded(self):
        daemon = FakeCodexDaemon(self.sock)
        proc = self.spawn_codex("--approvals", "never")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/start")[0],
                         {"cwd": str(self.work.resolve()), "sandbox": "read-only", "approvalPolicy": "never"})
        self.assertEqual(self.records()[0]["approvals"], "never")
        self.assertEqual(self.spawn_codex("--approvals", "bypass").returncode, 2)  # no full-access choice exists
        self.assertEqual(self.spawn_claude("--approvals", "never").returncode, 2)
        self.assertNotIn("--bg", self.claude_argv())

    def test_spawn_claude_records_no_codex_approvals(self):
        self.assertEqual(self.spawn_claude().returncode, 0)
        self.assertIsNone(self.records()[0]["approvals"])

    def test_protected_folders_and_their_parents_are_refused(self):
        daemon = FakeCodexDaemon(self.sock)
        relocated = self.tmp / "relocated-config"
        relocated.mkdir()
        link = self.home / "projects" / "link-into-config"
        link.symlink_to(self.home / ".claude")
        tools_root = SPAWN.resolve().parents[1]
        folders = [tools_root, tools_root / "bin", tools_root.parent,  # the tools, inside them, their parent
                   self.home / ".claude", self.home / ".codex", self.home,  # config folders and the home above them
                   relocated, link]
        for folder in folders:
            with self.subTest(folder=str(folder)):
                proc = self.run_tool(SPAWN, "--cwd", str(folder), "codex", "peer-check", "claude/t",
                                     CLAUDE_CONFIG_DIR=str(relocated))
                self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(daemon.calls, [])
        self.assertEqual(self.records(), [])

    def test_spawn_codex_queues_first_message_to_the_new_thread(self):
        FakeCodexDaemon(self.sock)
        proc = self.run_tool(SPAWN, "--cwd", str(self.work), "codex", "peer-check", "claude/t", "Hello there")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = (self.tmp / "codex-argv.txt").read_text()
        self.assertIn(THREAD_ID, argv)
        self.assertIn("Hello there", argv)

    def test_spawn_codex_refuses_a_duplicate_name(self):
        daemon = FakeCodexDaemon(self.sock, existing_names=["peer-check"])
        self.assertEqual(self.spawn_codex().returncode, 3)
        self.assertNotIn("thread/start", daemon.methods())
        self.assertEqual(self.records(), [])

    def test_spawn_codex_without_daemon_exits_4(self):
        self.assertEqual(self.spawn_codex().returncode, 4)
        self.assertEqual(self.records(), [])

    def test_naming_failure_archives_and_leaves_a_full_record(self):
        daemon = FakeCodexDaemon(self.sock, fail=["thread/name/set"])
        self.assertEqual(self.spawn_codex().returncode, 4)
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD_ID}])
        self.assertEqual([(r["event"], r["id"]) for r in self.records()],
                         [("spawned", THREAD_ID), ("retired", THREAD_ID)])

    def test_naming_and_archive_failure_still_leaves_a_retirable_record(self):
        daemon = FakeCodexDaemon(self.sock, fail=["thread/name/set", "thread/archive"])
        self.assertEqual(self.spawn_codex().returncode, 4)
        self.assertEqual([r["event"] for r in self.records()], ["spawned"])
        daemon.fail.clear()
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")  # the unnamed thread can still be retired
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unwritable_record_creates_nothing(self):
        daemon = FakeCodexDaemon(self.sock)
        proc = self.run_tool(SPAWN, "--cwd", str(self.work), "codex", "peer-check", "claude/t",
                             AGENT_COMMS_SPAWN_LOG=str(self.work))  # a folder cannot be appended to
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(daemon.calls, [])

    # --- spawn claude ---
    def test_spawn_claude_read_only_removes_write_tools_and_ends_options(self):
        proc = self.spawn_claude()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.claude_argv()
        self.assertIn(f"{self.work.resolve()}\n--bg\n-n\npeer-check\n--permission-mode\nplan\n"
                      "--disallowedTools\nEdit Write NotebookEdit Bash\n--\n"
                      "Teammate message from codex/t — not user approval\n", argv)
        self.assertNotIn("bypassPermissions", argv)
        (rec,) = self.records()
        self.assertEqual((rec["kind"], rec["id"], rec["access"]), ("claude", "ab12cd34", "read-only"))

    def test_spawn_claude_write_is_accept_edits_never_bypass(self):
        proc = self.spawn_claude("--write", "--approval", "owner, chat 2026-01-01")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.claude_argv()
        self.assertIn("--permission-mode\nacceptEdits\n--\n", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("dangerously", argv)
        self.assertEqual(self.records()[0]["access"], "write")

    def test_spawn_claude_refuses_a_duplicate_name(self):
        self.assertEqual(self.spawn_claude().returncode, 0)
        self.assertEqual(self.spawn_claude().returncode, 3)
        self.assertEqual(self.claude_argv().count("--bg"), 1)
        self.assertEqual(len(self.records()), 1)

    def test_spawn_claude_never_listed_is_still_recorded(self):
        proc = self.spawn_claude(STUB_NEVER_LISTED="1")
        self.assertEqual(proc.returncode, 4)
        (rec,) = self.records()
        self.assertEqual((rec["id"], rec["listed"]), ("ab12cd34", False))

    def test_spawn_claude_listing_failure_after_launch_is_still_recorded(self):
        proc = self.spawn_claude(STUB_AGENTS_FAIL_AFTER_LAUNCH="1")
        self.assertEqual(proc.returncode, 4)
        self.assertNotIn("Traceback", proc.stderr)
        (rec,) = self.records()
        self.assertEqual((rec["id"], rec["listed"], rec["id_guessed"]), ("ab12cd34", False, True))

    def test_spawn_claude_id_guess_skips_plain_numbers(self):
        proc = self.spawn_claude(STUB_NEVER_LISTED="1", STUB_BG_OUTPUT="Started on 20260101 as ab12cd34")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.records()[0]["id"], "ab12cd34")

    def test_spawn_claude_ignores_a_session_in_another_folder(self):
        proc = self.spawn_claude(STUB_REGISTER_CWD="/somewhere/else")
        self.assertEqual(proc.returncode, 4)
        self.assertIs(self.records()[0]["listed"], False)

    def test_spawn_claude_needs_a_first_message(self):
        proc = self.run_tool(SPAWN, "--cwd", str(self.work), "claude", "peer-check", "codex/t")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)
        self.assertNotIn("--bg", self.claude_argv())

    def test_dry_run_contacts_nothing(self):
        daemon = FakeCodexDaemon(self.sock)
        for kind in ("codex", "claude"):
            proc = self.run_tool(SPAWN, "--dry-run", "--cwd", str(self.work), kind, "peer-check", "x/t", "Hi")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DRY RUN", proc.stdout)
        self.assertEqual(daemon.calls, [])
        self.assertEqual(self.claude_argv(), "")
        self.assertEqual(self.records(), [])

    # --- retire ---
    def test_retire_refuses_a_session_it_did_not_create(self):
        daemon = FakeCodexDaemon(self.sock)
        proc = self.run_tool(RETIRE, "0a0a0a0a-0000-7000-8000-00000000ffff", "claude/t")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(daemon.calls, [])
        self.assertEqual(self.claude_argv(), "")

    def test_retire_refuses_when_the_live_session_no_longer_matches(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        daemon.threads[THREAD_ID]["name"] = "someone-elses-thread"  # the id now points at something else
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(daemon.params("thread/archive"), [])
        daemon.threads[THREAD_ID].update(name="peer-check", cwd="/somewhere/else")
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)
        self.assertEqual(daemon.params("thread/archive"), [])

    def test_retire_refuses_when_the_daemon_returns_another_thread(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        daemon.threads[THREAD_ID]["id"] = "some-other-thread"
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)
        self.assertEqual(daemon.params("thread/archive"), [])

    def test_retire_codex_archives_by_default(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD_ID}])
        self.assertEqual(daemon.params("thread/delete"), [])
        self.assertEqual(self.records()[-1]["action"], "archive")
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)  # already archived

    def test_an_agent_resumed_from_the_menu_can_be_retired_again(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 0)
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)   # already archived
        daemon.archived.discard(THREAD_ID)
        with open(self.spawn_log, "a") as fh:                                       # what the menu writes
            fh.write(json.dumps({"event": "resumed", "kind": "codex", "id": THREAD_ID}) + "\n")
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)                              # only the latest step counts
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD_ID}] * 2)

    def test_retire_dry_run_changes_nothing(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        before = len(daemon.calls)
        proc = self.run_tool(RETIRE, "--dry-run", THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(daemon.calls), before)
        self.assertEqual(len(self.records()), 1)

    def test_delete_needs_recorded_approval(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        self.assertEqual(self.run_tool(RETIRE, "--delete", THREAD_ID, "claude/t").returncode, 2)
        self.assertEqual(self.run_tool(RETIRE, "--delete", "--approval", "  ", THREAD_ID, "claude/t").returncode, 2)
        self.assertEqual(daemon.params("thread/delete"), [])
        proc = self.run_tool(RETIRE, "--delete", "--approval", "owner, chat 2026-01-01 18:00Z", THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/delete"), [{"threadId": THREAD_ID}])
        self.assertEqual(self.records()[-1]["approval"], "owner, chat 2026-01-01 18:00Z")
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)  # already deleted

    def test_retire_survives_a_torn_record_line(self):
        daemon = FakeCodexDaemon(self.sock)
        self.spawn_codex()
        with open(self.spawn_log, "a") as fh:
            fh.write('{"truncated": \n')
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD_ID}])

    def test_retire_claude_stops_and_a_failure_records_nothing(self):
        self.spawn_claude()
        failed = self.run_tool(RETIRE, "ab12cd34", "codex/t", STUB_FAIL="1")
        self.assertEqual(failed.returncode, 4)
        self.assertEqual(len(self.records()), 1)
        proc = self.run_tool(RETIRE, "ab12cd34", "codex/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("\nstop\nab12cd34\n", self.claude_argv())
        self.assertEqual(self.records()[-1]["action"], "stop")

    def test_retire_claude_records_a_finished_session_without_stopping_it(self):
        """A session that already stopped is listed by `--all` only. Retiring it writes the record and
        runs no `claude stop`, which has nothing to stop."""
        self.spawn_claude()
        state = self.tmp / "sessions.json"
        (self.tmp / "finished.json").write_text(state.read_text())
        state.write_text("[]")
        proc = self.run_tool(RETIRE, "ab12cd34", "owner/agent-menu")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("\nstop\n", self.claude_argv())
        self.assertEqual((self.records()[-1]["action"], self.records()[-1]["already_stopped"]), ("stop", True))

    def test_retire_claude_does_not_take_an_unreadable_listing_for_a_stopped_session(self):
        self.spawn_claude()
        proc = self.run_tool(RETIRE, "ab12cd34", "owner/agent-menu", STUB_PLAIN_AGENTS_FAIL="1")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(len(self.records()), 1)                              # only the spawn record
        self.assertNotIn("\nstop\n", self.claude_argv())

    def test_retire_claude_takes_a_session_listed_twice_as_one(self):
        """The CLI lists a session twice when a finished run sits next to a running one."""
        self.spawn_claude()
        state = self.tmp / "sessions.json"
        (self.tmp / "finished.json").write_text(json.dumps([dict(json.loads(state.read_text())[0], state="done")]))
        proc = self.run_tool(RETIRE, "ab12cd34", "owner/agent-menu")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("\nstop\nab12cd34\n", self.claude_argv())

    def test_retire_claude_expect_stopped_refuses_a_running_session(self):
        self.spawn_claude()
        proc = self.run_tool(RETIRE, "--expect-stopped", "ab12cd34", "owner/agent-menu")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertNotIn("\nstop\n", self.claude_argv())
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.run_tool(RETIRE, "--expect-stopped", "--delete", "--approval", "x", "ab12cd34",
                                       "owner/agent-menu").returncode, 2)

    def test_retire_again_only_when_the_agent_runs_again(self):
        """A retired thread that is still archived is refused. One unarchived by hand, which leaves no
        record, is running again and may be retired again."""
        daemon = FakeCodexDaemon(self.sock)
        self.assertEqual(self.spawn_codex().returncode, 0)
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 0)
        self.assertEqual(self.run_tool(RETIRE, THREAD_ID, "claude/t").returncode, 3)       # still archived
        daemon.archived.discard(THREAD_ID)                                                    # `codex unarchive` by hand
        proc = self.run_tool(RETIRE, THREAD_ID, "claude/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(daemon.params("thread/archive"), [{"threadId": THREAD_ID}] * 2)

    def test_retire_claude_rm_passes_no_force_flags(self):
        self.spawn_claude()
        proc = self.run_tool(RETIRE, "--delete", "--approval", "owner, chat", "ab12cd34", "codex/t")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.claude_argv()
        self.assertIn("\nrm\nab12cd34\n--\n", argv)
        self.assertNotIn("--discard-unpushed", argv)
        self.assertNotIn("--force-remove-worktree", argv)

    def test_retire_claude_refuses_a_reused_short_id(self):
        self.spawn_claude()
        state = self.tmp / "sessions.json"
        sessions = json.loads(state.read_text())
        original = dict(sessions[0])
        for changed in ([dict(original, name="some other session")],  # the short id now belongs to another session
                        [dict(original, name=None)],                  # unnamed is accepted for Codex only
                        [original, dict(original, sessionId="ab12cd34-0000-4000-8000-00000000ffff")]):  # two sessions share the id
            with self.subTest(changed=changed):
                state.write_text(json.dumps(changed))
                self.assertEqual(self.run_tool(RETIRE, "ab12cd34", "codex/t").returncode, 3)
        self.assertNotIn("\nstop\n", self.claude_argv())


if __name__ == "__main__":
    unittest.main()
