"""Codex threads, from the local app-server daemon."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from codex_ws import CodexError, CodexWS  # noqa: E402

from .base import SourceError  # noqa: E402

STATES = {"active": "working", "idle": "idle", "notLoaded": "stopped"}


def _thread(raw):
    status = raw.get("status")
    status = status.get("type") if isinstance(status, dict) else status
    return {"kind": "codex", "session_id": raw["id"], "short_id": raw["id"][:8],
            "name": raw.get("name") or raw["id"][:8], "cwd": raw.get("cwd"), "pid": None,
            "state": STATES.get(status, "unknown"), "background": False}


def list_threads(extra_ids=(), timeout=1.5):
    """Listed threads, plus `extra_ids` read one by one. A thread that `spawn-peer` just created
    is missing from the listing until its first turn, so the caller passes recorded ids here."""
    try:
        ws = CodexWS(timeout=timeout)
    except CodexError as exc:
        raise SourceError(f"codex: {exc}") from None
    try:
        listing = ws.rpc("thread/list", {"limit": 200}).get("data", [])
        threads = {t["id"]: _thread(t) for t in listing if isinstance(t, dict) and t.get("id")}
        for thread_id in extra_ids:
            if thread_id in threads:
                continue
            try:
                raw = ws.rpc("thread/read", {"threadId": thread_id}).get("thread")
            except CodexError:
                continue  # archived, deleted or never loaded: not an error for the whole source
            if isinstance(raw, dict) and raw.get("id"):
                threads[raw["id"]] = _thread(raw)
    except CodexError as exc:
        raise SourceError(f"codex: {exc}") from None
    finally:
        ws.close()
    return list(threads.values())
