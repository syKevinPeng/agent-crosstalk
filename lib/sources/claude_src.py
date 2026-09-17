"""Claude Code sessions, from `claude agents --json`."""
import json
import os
import subprocess

from .base import SourceError

STATES = {"busy": "working", "idle": "idle", "waiting": "needs_owner"}


def list_sessions(timeout=1.5):
    """Return one dict per live session. `state` is `unknown` when the CLI reports none,
    which is the case for sessions opened directly in a terminal."""
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        out = subprocess.run([binary, "agents", "--json"], capture_output=True, text=True, timeout=timeout)
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
        if not isinstance(entry, dict) or not entry.get("sessionId") or entry["sessionId"] in seen:
            continue
        seen.add(entry["sessionId"])
        state = STATES.get(entry.get("status"), "unknown")
        if entry.get("state") == "blocked":
            state = "needs_owner"
        sessions.append({"kind": "claude", "session_id": entry["sessionId"], "short_id": entry.get("id"),
                         "name": entry.get("name") or entry["sessionId"][:8], "cwd": entry.get("cwd"),
                         "pid": entry.get("pid"), "state": state, "background": entry.get("kind") == "background"})
    return sessions
