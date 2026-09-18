"""Codex threads, from the local app-server daemon."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from codex_ws import CodexError, CodexWS  # noqa: E402

from .base import SourceError, clean  # noqa: E402

STATES = {"active": "working", "idle": "idle", "notLoaded": "stopped"}
# An active thread that is blocked on the owner says so in its status (codex-cli 0.154.0 ThreadStatus:
# {type: "active", activeFlags: [...]}). It must read as waiting, not working, pane or no pane.
WAITING_FLAGS = {"waitingOnApproval", "waitingOnUserInput"}


def _state(status):
    flags = status.get("activeFlags") if isinstance(status, dict) else None
    kind = status.get("type") if isinstance(status, dict) else status
    if isinstance(flags, list) and WAITING_FLAGS & {flag for flag in flags if isinstance(flag, str)}:
        return "needs_owner"
    return STATES.get(kind, "unknown") if isinstance(kind, str) else "unknown"


def _thread(raw):
    name = clean(raw.get("name")).strip() or raw["id"][:8]                        # never a blank row
    return {"kind": "codex", "session_id": raw["id"], "short_id": raw["id"][:8],
            "name": name, "cwd": clean(raw.get("cwd")), "pid": None, "model": clean(raw.get("model")),
            "state": _state(raw.get("status")), "background": False, "quit": False}


def list_threads(extra_ids=(), timeout=1.5, include_quit=False):
    """Listed threads, plus `extra_ids` read one by one. A thread that `spawn-peer` just created
    is missing from the listing until its first turn, so the caller passes recorded ids here.
    With `include_quit`, archived threads are added too, marked quit: archiving is how a Codex
    thread is quit, and `thread/unarchive` brings it back."""
    try:
        ws = CodexWS(timeout=timeout)
    except CodexError as exc:
        raise SourceError(f"codex: {exc}") from None
    try:
        listing = ws.rpc("thread/list", {"limit": 200}).get("data")
        if not isinstance(listing, list):
            raise SourceError("codex: thread/list gave no list")   # this source fails, not the whole refresh
        threads = {t["id"]: _thread(t) for t in listing if isinstance(t, dict) and isinstance(t.get("id"), str)}
        if include_quit:
            archived = ws.rpc("thread/list", {"limit": 200, "archived": True}).get("data", [])
            for raw in archived:
                if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                    threads[raw["id"]] = dict(_thread(raw), quit=True, state="stopped")
        for thread_id in extra_ids:
            if thread_id in threads:
                continue                     # already listed, possibly as quit
            try:
                raw = ws.rpc("thread/read", {"threadId": thread_id}).get("thread")
            except CodexError:
                continue  # archived, deleted or never loaded: not an error for the whole source
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                threads[raw["id"]] = _thread(raw)
    except CodexError as exc:
        raise SourceError(f"codex: {exc}") from None
    finally:
        ws.close()
    return list(threads.values())
