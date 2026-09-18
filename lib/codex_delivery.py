"""Make sure a message queued for a Codex thread is taken by a turn, and say so when it is not.

`codex queue` only stores a message. The daemon starts a turn from its queue only while it has the
thread loaded, and it unloads a thread about a minute after the thread goes idle with no client
attached. A spawned agent never has a client, so every message sent after its first minute used to
stay in the queue until someone opened the thread, and nothing said so.

prepare() runs before `codex queue`. It loads an unloaded agent that spawn-peer created, with the
sandbox and approvals its spawn record holds. confirm() runs after, and watches the daemon's queue
until a turn takes the message. A thread with no spawn record is never woken: its settings are
unknown, and a wake with the daemon's defaults could widen them.

Checked against codex-cli 0.154.0. `thread/queue/list` is experimental, so recheck after upgrades.
Run as a script, it prints one JSON object for bin/send-to-codex."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import spawn_log  # noqa: E402
from codex_ws import CodexError, CodexWS  # noqa: E402

DEFAULT_WAIT = 10.0  # seconds confirm() waits for an idle loaded thread to take the message
POLL = 0.25
# What confirm() can say. The first two mean a turn has the message or will get it without help.
DELIVERED = ("started", "waiting")


def status_type(thread):
    status = thread.get("status") if isinstance(thread, dict) else None
    return status.get("type") if isinstance(status, dict) else status


def read_thread(ws, thread_id):
    thread = ws.rpc("thread/read", {"threadId": thread_id}).get("thread")
    return thread if isinstance(thread, dict) else None


def resolve(ws, thread):
    """The thread for `thread`, an id or an exact name, or None when it names no single thread."""
    try:
        found = read_thread(ws, thread)
    except CodexError:
        found = None
    if found and found.get("id"):
        return found
    listing = ws.rpc("thread/list", {"searchTerm": thread, "limit": 100})
    named = [t for t in listing.get("data", []) if isinstance(t, dict) and t.get("name") == thread and t.get("id")]
    return read_thread(ws, named[0]["id"]) if len(named) == 1 else None


def wake_params(thread_id):
    """(thread/resume params, None) for a live Codex agent that spawn-peer created, else (None, why not)."""
    events = spawn_log.events_for(thread_id)
    origin = next((e for e in events if e.get("event") == "spawned" and e.get("kind") == "codex"), None)
    if origin is None:
        return None, "spawn-peer did not create it, so its sandbox and approvals are unknown"
    # Only the latest step counts, as in retire-peer: an agent resumed from the menu is live again.
    lifecycle = [e for e in events if e.get("event") in ("retired", "resumed")]
    if lifecycle and lifecycle[-1].get("event") == "retired":
        return None, "it is retired. Resume it first"
    sandbox = spawn_log.CODEX_SANDBOX.get(origin.get("access"))
    approvals = spawn_log.CODEX_APPROVALS.get(origin.get("approvals"))
    if not sandbox or not approvals or not origin.get("cwd"):
        return None, "its spawn record does not say which folder, sandbox and approvals it runs with"
    return {"threadId": thread_id, "cwd": origin["cwd"], "sandbox": sandbox, **approvals, "excludeTurns": True}, None


def prepare(thread):
    """Before `codex queue`: {thread_id, thread_status, woke, note}. Raises CodexError."""
    out = {"thread_id": None, "thread_status": None, "woke": False, "note": None}
    ws = CodexWS()
    try:
        found = resolve(ws, thread)
        if found is None:
            out["note"] = "the daemon knows no single thread by that id or name"
            return out
        out["thread_id"], out["thread_status"] = found["id"], status_type(found)
        if out["thread_status"] == "notLoaded":
            params, out["note"] = wake_params(found["id"])
            if params:
                ws.rpc("thread/resume", params)
                out["woke"] = True
    finally:
        ws.close()
    return out


def is_ours(item, queued_id, marker):
    """Is this queue item the message we queued? By the id `codex queue` printed, or, because Codex does
    not document that the queue lists that same id, by a marker in its text: our Msg-ID line, or the
    instruction itself. A miss on both would report a waiting message as taken, so both are tried."""
    if not isinstance(item, dict):
        return False
    if queued_id and queued_id in (item.get("id"), item.get("clientUserMessageId")):
        return True
    texts = [part.get("text") for part in item.get("input") or [] if isinstance(part, dict)]
    return bool(marker) and any(isinstance(t, str) and marker in t for t in texts)


def confirm(thread_id, queued_id, woke=False, wait=DEFAULT_WAIT, marker=None):
    """After `codex queue`: {delivery, woke, note}. `marker` is text only our message holds.
    `delivery` is one of
    started     a turn took the message
    waiting     the thread is running a turn, and takes the message when that turn ends
    not-loaded  nothing will run it until the thread is opened
    stuck       the thread is loaded and idle, and the message was still queued after `wait` seconds
    Raises CodexError, which the caller reports as `unknown`."""
    ws = CodexWS(experimental=True)
    try:
        deadline = time.monotonic() + wait
        while True:
            items = ws.rpc("thread/queue/list", {"threadId": thread_id}).get("data", [])
            if not any(is_ours(item, queued_id, marker) for item in items):
                return {"delivery": "started", "woke": woke, "note": None}
            status = status_type(read_thread(ws, thread_id))
            if status == "active":
                return {"delivery": "waiting", "woke": woke, "note": None}
            if status == "notLoaded":
                # The daemon can unload the thread between prepare() and the queue.
                params, why = (None, "it was woken, and unloaded again") if woke else wake_params(thread_id)
                if not params:
                    return {"delivery": "not-loaded", "woke": woke, "note": why}
                ws.rpc("thread/resume", params)
                woke = True
                continue
            if time.monotonic() >= deadline:
                return {"delivery": "stuck", "woke": woke, "note": f"thread status {status}"}
            time.sleep(POLL)
    finally:
        ws.close()


def main(argv):
    """`prepare <thread>` or `confirm <thread-id> <queued-id> <woke 0|1> <wait seconds> [marker]`. Prints JSON."""
    try:
        if argv[:1] == ["prepare"] and len(argv) == 2:
            out = prepare(argv[1])
        elif argv[:1] == ["confirm"] and len(argv) in (5, 6):
            out = confirm(argv[1], argv[2], woke=argv[3] == "1", wait=float(argv[4]),
                          marker=argv[5] if len(argv) == 6 else None)
        else:
            print(main.__doc__, file=sys.stderr)
            return 2
    except (CodexError, OSError, ValueError, KeyError, TypeError) as exc:
        out = {"error": str(exc)[:300] or type(exc).__name__}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
