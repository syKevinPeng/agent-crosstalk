"""Claude Code sessions, from `claude agents --json`, and each one's model from its transcript."""
import glob
import json
import os
import re
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

# The listing names no model. Each session's transcript records one on every assistant turn, so the
# model is the last one there. Only the tail is read, and only when the file has changed.
TRANSCRIPT_TAIL = 256 * 1024
_models = {}          # session id -> (transcript path, (size, mtime), model)


def _projects_folder():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")


def short_model(model):
    """`claude-opus-5-5` -> `opus-5.5`, `claude-haiku-4-5-20251001` -> `haiku-4.5`: it fits the column."""
    model = re.sub(r"-\d{8}$", "", re.sub(r"^claude-", "", model))
    return re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)


def _last_model(path):
    with open(path, "rb") as fh:
        fh.seek(max(0, os.fstat(fh.fileno()).st_size - TRANSCRIPT_TAIL))
        lines = fh.read().split(b"\n")
    for line in reversed(lines[1:] if len(lines) > 1 else lines):  # the first line may be cut
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):   # `<synthetic>` is no model
            return clean(short_model(model))
    return ""


def model_of(session_id):
    """The model a session last answered with, or "" when its transcript has none or cannot be read."""
    path, seen, model = _models.get(session_id, (None, None, ""))
    try:
        if not path or not os.path.exists(path):
            found = glob.glob(os.path.join(glob.escape(_projects_folder()), "*", glob.escape(session_id) + ".jsonl"))
            path = found[0] if found else None
        if not path:
            return ""
        st = os.stat(path)
        if (st.st_size, st.st_mtime_ns) != seen:
            seen, model = (st.st_size, st.st_mtime_ns), _last_model(path) or model
    except OSError:
        return model
    _models[session_id] = (path, seen, model)
    return model


# A session that has ended leaves its name and folder in its transcript. The menu asks for them when a
# spawn record names a creator that no longer runs, so a restarted session can take over its children.
# An ended transcript does not change, so each is read once, and only its title and folder lines are parsed.
_identities = {}      # session id -> (name, cwd), ("", "") when there is no transcript


def identity_of(session_id):
    """The last name and the folder of a Claude session, from its transcript. ("", "") if unknown."""
    if session_id in _identities:
        return _identities[session_id]
    name, cwd = "", ""
    try:
        found = glob.glob(os.path.join(glob.escape(_projects_folder()), "*", glob.escape(session_id) + ".jsonl"))
        if found:
            with open(found[0], "rb") as fh:
                for line in fh:
                    wanted = b'"agent-name"' in line or b'"custom-title"' in line
                    if not wanted and (cwd or b'"cwd"' not in line):
                        continue
                    try:
                        entry = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if not cwd and isinstance(entry.get("cwd"), str):
                        cwd = entry["cwd"]
                    title = entry.get("agentName") if entry.get("type") == "agent-name" else \
                        entry.get("customTitle") if entry.get("type") == "custom-title" else None
                    if isinstance(title, str) and title:
                        name = title
    except OSError:
        return "", ""
    _identities[session_id] = (clean(name), cwd)
    return _identities[session_id]


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
    live = [dict(s, quit=False, model=model_of(s["session_id"])) for s in _listing(timeout=timeout)]
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
