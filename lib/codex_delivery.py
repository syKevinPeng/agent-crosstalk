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


def queue_items(ws, thread_id):
    """The thread's queued messages. An answer in any other shape is an error, never an empty queue:
    `thread/queue/list` is experimental, and reading a changed answer as "nothing queued" would report
    every waiting message as taken. `ws` must have been opened with experimental=True."""
    items = ws.rpc("thread/queue/list", {"threadId": thread_id}).get("data")
    if not isinstance(items, list):
        raise CodexError("thread/queue/list: the answer has no list of queued messages")
    return items


def turn_took(ws, thread_id, queued_id, marker):
    """Does one of the thread's latest turns hold our message? A turn can end before anyone sees it
    running, as one that fails at once on a usage limit or an expired login does."""
    for turn in ws.rpc("thread/turns/list", {"threadId": thread_id, "limit": 3, "sortDirection": "desc"}).get("data") or []:
        for item in (turn.get("items") or []) if isinstance(turn, dict) else []:
            if isinstance(item, dict) and item.get("type") == "userMessage" and is_ours(
                    {"id": item.get("clientId"), "input": item.get("content")}, queued_id, marker):
                return True
    return False


def wake_params(thread_id):
    """(thread/resume params, None) for a live Codex agent that spawn-peer created, else (None, why not)."""
    events = spawn_log.events_for(thread_id)
    # The latest record counts, as in retire-peer.
    origin = next((e for e in reversed(events) if e.get("event") == "spawned" and e.get("kind") == "codex"), None)
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


def wake(ws, thread_id):
    """Load an unloaded agent that spawn-peer created, with its recorded settings. Returns (woke, why not)."""
    params, why = wake_params(thread_id)
    if not params:
        return False, why
    # The spawn record misses an archive done by hand, or one whose record line failed. The daemon
    # refuses to list an archived thread's queue and says so. No answer at all is no wake either.
    try:
        queue_items(ws, thread_id)
    except CodexError as exc:
        if "is archived" in str(exc):
            return False, "it is archived. Resume it first"
        return False, f"it could not be checked that it is not archived ({exc})"[:300]
    ws.rpc("thread/resume", params)
    return True, None


def prepare(thread):
    """Before `codex queue`: {thread_id, thread_status, woke, note}. Raises CodexError."""
    out = {"thread_id": None, "thread_status": None, "woke": False, "note": None}
    ws = CodexWS(experimental=True)
    try:
        found = resolve(ws, thread)
        if found is None:
            out["note"] = "the daemon knows no single thread by that id or name"
            return out
        out["thread_id"], out["thread_status"] = found["id"], status_type(found)
        if out["thread_status"] == "notLoaded":
            out["woke"], out["note"] = wake(ws, found["id"])
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
    started     the message left the queue, and one of the latest turns holds it or a turn is running.
                Leaving the queue alone is not enough: a message can leave it with no turn taking it
    waiting     the thread is running a turn, and takes the message when that turn ends
    not-loaded  nothing will run it until the thread is opened
    stuck       the thread is loaded and idle, and the message was still queued after `wait` seconds
    unknown     it left the queue, but no running turn was seen before `wait` ran out
    Raises CodexError, which the caller reports as `unknown`."""
    ws = CodexWS(experimental=True)
    try:
        deadline = time.monotonic() + wait
        while True:
            queued = any(is_ours(item, queued_id, marker) for item in queue_items(ws, thread_id))
            status = status_type(read_thread(ws, thread_id))
            if status == "active":
                # A running turn with our message gone is taken as the turn that took it. Checking that
                # the turn holds it would be stronger, but no mechanism is known that removes a message
                # without a turn, and a turn may list its message late.
                return {"delivery": "waiting" if queued else "started", "woke": woke, "note": None}
            if not queued and turn_took(ws, thread_id, queued_id, marker):
                return {"delivery": "started", "woke": woke, "note": None}
            if queued and status == "notLoaded":
                # The daemon can unload the thread between prepare() and the queue.
                done, why = (False, "it was woken, and unloaded again") if woke else wake(ws, thread_id)
                if not done:
                    return {"delivery": "not-loaded", "woke": woke, "note": why}
                woke = True
                continue
            if time.monotonic() >= deadline:
                if queued:
                    return {"delivery": "stuck", "woke": woke, "note": f"thread status {status}"}
                return {"delivery": "unknown", "woke": woke,
                        "note": f"it left the queue, but no turn was seen running (thread status {status})"}
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
    except Exception as exc:  # noqa: BLE001 -- any failure is an unanswered check, reported as unknown
        out = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
