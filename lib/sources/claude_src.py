"""Claude Code sessions, from `claude agents --json`."""
import json
import os
import subprocess
import time

from .base import SourceError, clean

STATES = {"busy": "working", "idle": "idle", "waiting": "needs_owner"}
# Each `claude` start costs about a tenth of a second of CPU, and the listing is read every two
# seconds. The `--all` lookup for finished spawned sessions is therefore kept, and redone only when
# another id goes missing, or after FINISHED_EVERY seconds.
FINISHED_EVERY = 60.0
_finished = {"missing": frozenset(), "at": float("-inf"), "sessions": []}


def _listing(extra_args=(), timeout=1.5):
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        out = subprocess.run([binary, "agents", "--json", *extra_args], capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"claude: {type(exc).__name__}") from None
    if out.returncode != 0:
        raise SourceError(f"claude: exit {out.returncode}")
    try:
        data = json.loads(out.stdout or "[]")
    except ValueError:
        raise SourceError("claude: output is not JSON") from None
    sessions, seen = [], set()
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str) or entry["sessionId"] in seen:
            continue
        text = {k: entry.get(k) if isinstance(entry.get(k), str) else "" for k in ("id", "name", "cwd")}
        seen.add(entry["sessionId"])
        status = entry.get("status")
        state = STATES.get(status, "unknown") if isinstance(status, str) else "unknown"
        if entry.get("state") == "blocked":
            state = "needs_owner"
        name = clean(text["name"]).strip() or clean(text["id"]) or clean(entry["sessionId"][:8])  # never blank
        sessions.append({"kind": "claude", "session_id": entry["sessionId"], "short_id": text["id"],
                         "name": name, "cwd": clean(text["cwd"]),
                         "pid": entry.get("pid") if isinstance(entry.get("pid"), int) else None,
                         "state": state, "background": entry.get("kind") == "background"})
    return sessions


def list_sessions(extra_ids=(), timeout=1.5):
    """Return one dict per live session. `state` is `unknown` when the CLI reports none,
    which is the case for sessions opened directly in a terminal.

    `extra_ids` are the short ids of sessions that spawn-peer created. A spawned session that has
    finished drops out of the plain listing but is still spawn-peer's to retire, so any of them
    missing here are looked up in `--all`, and only those are added."""
    sessions = _listing(timeout=timeout)
    missing = frozenset(extra_ids) - {s["short_id"] for s in sessions}
    if missing:
        listed = {s["session_id"] for s in sessions}
        for session in _finished_sessions(missing, timeout):
            if session["short_id"] in missing and session["session_id"] not in listed:
                sessions.append(session)
                listed.add(session["session_id"])
    return sessions


def _finished_sessions(missing, timeout):
    """`claude agents --json --all`, kept between refreshes (see FINISHED_EVERY)."""
    now = time.monotonic()
    if missing != _finished["missing"] or now - _finished["at"] >= FINISHED_EVERY:
        try:
            _finished.update(missing=missing, at=now, sessions=_listing(("--all",), timeout=timeout))
        except SourceError:
            pass               # the live listing stands: keep the last lookup and retry next refresh
    return _finished["sessions"]
