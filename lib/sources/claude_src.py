"""Claude Code sessions, from `claude agents --json`."""
import json
import os
import subprocess
import time

from .base import SourceAbsent, SourceError, clean, usage_folder

STATES = {"busy": "working", "idle": "idle", "waiting": "needs_owner"}
# The listing runs off the draw path, so it can wait for a slow start. At 1.5 s a busy machine
# emptied every Claude row now and then.
LIST_TIMEOUT = 5.0

# Each `claude` start costs about a tenth of a second of CPU, and the listing is read every two
# seconds. The `--all` lookup for finished spawned sessions is therefore kept, and redone only when
# another id goes missing, or after FINISHED_EVERY seconds.
FINISHED_EVERY = 60.0
_finished = {"missing": frozenset(), "at": float("-inf"), "sessions": []}


def _listing(extra_args=(), timeout=LIST_TIMEOUT):
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        out = subprocess.run([binary, "agents", "--json", *extra_args], capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        raise SourceAbsent("claude: not installed") from None
    except subprocess.TimeoutExpired:
        raise SourceError(f"claude: timed out after {timeout:g} s") from None
    except OSError as exc:
        raise SourceError(f"claude: {type(exc).__name__}") from None
    if out.returncode != 0:
        raise SourceError(f"claude: exit {out.returncode}")
    try:
        data = json.loads(out.stdout or "[]")
    except ValueError:
        raise SourceError("claude: output is not JSON") from None
    if not isinstance(data, list):
        raise SourceError("claude: the listing is not a list")   # a changed format, never an empty tree
    sessions, seen = [], set()
    own = os.path.realpath(usage_folder())
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str) or entry["sessionId"] in seen:
            continue
        text = {k: entry.get(k) if isinstance(entry.get(k), str) else "" for k in ("id", "name", "cwd")}
        if text["cwd"] and os.path.realpath(text["cwd"]) == own:
            continue                                  # the menu's own usage call, which lasts a second or two
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


def list_sessions(extra_ids=(), timeout=LIST_TIMEOUT, include_quit=False, errors=None):
    """One dict per live session, plus stopped ones in two cases, each marked `quit`.

    What counts as live is the CLI's own active list (`claude agents --json`). A session has stopped
    when `--all` lists it and the active list does not. The `state` field cannot tell this apart:
    `done` only means the session's last task finished, and a running, idle session reports it too.

    Stopped sessions are added
    - all of them, with `include_quit`: the quit list and the popup ask for this, and
    - otherwise only those in `extra_ids`, the short ids spawn-peer created. A spawned session that
      finished is still part of its parent's work, so it stays in the tree. That lookup is cached
      (see FINISHED_EVERY), because the sidebar refreshes every two seconds.

    `state` is `unknown` when the CLI reports none, which is the case for sessions opened
    directly in a terminal.

    With an `errors` list, a failed lookup of stopped sessions adds a line there and the live
    sessions stand, so the reader sees that rows may be missing. Without one it raises, which is
    what an action wants before it touches a session."""
    live = [dict(s, quit=False) for s in _listing(timeout=timeout)]
    seen = {s["session_id"] for s in live}
    if include_quit:
        try:
            stopped = _listing(("--all",), timeout=timeout)
        except SourceError:
            if errors is None:
                raise
            stopped, _ = [], errors.append("claude: quit sessions unreadable")
    else:
        missing = frozenset(extra_ids) - {s["short_id"] for s in live}
        stopped = [s for s in _finished_sessions(missing, timeout, errors) if s["short_id"] in missing] if missing else []
    sessions = list(live)
    for session in stopped:
        if session["session_id"] not in seen:          # a live listing of the same session always wins
            seen.add(session["session_id"])
            sessions.append(dict(session, quit=True, state="stopped", pid=None))
    return sessions


def _finished_sessions(missing, timeout, errors=None):
    """`claude agents --json --all`, kept between refreshes (see FINISHED_EVERY). When a lookup
    fails, the last one stands if it was made for the same spawned ids. Otherwise a spawned
    session that finished may be missing, and `errors` says so."""
    now = time.monotonic()
    if missing != _finished["missing"] or now - _finished["at"] >= FINISHED_EVERY:
        try:
            _finished.update(missing=missing, at=now, sessions=_listing(("--all",), timeout=timeout))
        except SourceError:
            usable = _finished["missing"] == missing and _finished["at"] > float("-inf")
            if not usable and errors is not None:
                errors.append("claude: finished sessions unreadable")
    return _finished["sessions"]
