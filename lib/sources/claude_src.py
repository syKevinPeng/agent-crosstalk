"""Claude Code sessions, from `claude agents --json`."""
import json
import os
import subprocess

from .base import SourceError, clean

STATES = {"busy": "working", "idle": "idle", "waiting": "needs_owner"}


def _agents(binary, extra, timeout):
    try:
        out = subprocess.run([binary, "agents", "--json", *extra], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"claude: {type(exc).__name__}") from None
    if out.returncode != 0:
        raise SourceError(f"claude: exit {out.returncode}")
    try:
        data = json.loads(out.stdout or "[]")
    except ValueError:
        raise SourceError("claude: output is not JSON") from None
    return [e for e in (data if isinstance(data, list) else [])
            if isinstance(e, dict) and isinstance(e.get("sessionId"), str)]


def list_sessions(timeout=1.5, include_quit=False):
    """One dict per live session, plus, with `include_quit`, each session that has stopped.

    What counts as live is the CLI's own active list (`claude agents --json`). A session is quit when
    `--all` lists it and the active list does not. The `state` field cannot tell this apart: `done`
    only means the session's last task finished, and a running, idle session reports it too.
    `state` here is `unknown` when the CLI reports none, which is the case for sessions opened
    directly in a terminal."""
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    live = _agents(binary, [], timeout)
    live_ids = {e["sessionId"] for e in live}
    entries = live
    if include_quit:
        entries = live + [e for e in _agents(binary, ["--all"], timeout) if e["sessionId"] not in live_ids]
    sessions, seen = [], set()
    for entry in entries:
        if entry["sessionId"] in seen:
            continue
        seen.add(entry["sessionId"])
        text = {k: entry.get(k) if isinstance(entry.get(k), str) else "" for k in ("id", "name", "cwd")}
        quit = entry["sessionId"] not in live_ids
        state = "stopped" if quit else STATES.get(entry.get("status"), "unknown")
        if not quit and entry.get("state") == "blocked":
            state = "needs_owner"
        name = clean(text["name"]).strip() or text["id"] or entry["sessionId"][:8]   # never a blank row
        sessions.append({"kind": "claude", "session_id": entry["sessionId"], "short_id": text["id"],
                         "name": name, "cwd": clean(text["cwd"]),
                         "pid": entry.get("pid") if isinstance(entry.get("pid"), int) and not quit else None,
                         "state": state, "background": entry.get("kind") == "background", "quit": quit})
    return sessions
