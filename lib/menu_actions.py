"""What the owner can do from the menu: instruct, open a pane, attach, retire. Every action is
logged first-or-refused, and none of them has a command-line form: they run only inside the menu."""
import datetime
import fcntl
import json
import os
import subprocess

import auth_readers
from sources import tmux_src
from sources.base import SourceError

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


class Refused(Exception):
    """The action was not performed. The message says why and is shown to the owner."""


def log_path():
    return os.environ.get("AGENT_MENU_ACTIONS_LOG") or os.path.join(ROOT, "log", "menu-actions.jsonl")


def _check_log_writable():
    try:
        os.makedirs(os.path.dirname(log_path()) or ".", exist_ok=True)
        with open(log_path(), "a", encoding="utf-8"):
            pass
    except OSError:
        raise Refused("the action log cannot be written, so nothing was done") from None


def _log(agent, action, result, **extra):
    record = {"time_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "agent_key": agent.key, "agent_name": agent.name, "kind": agent.kind,
              "action": action, "result": result}
    record.update(extra)
    with open(log_path(), "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _same_pane(agent):
    """The pane must still be the process we matched. tmux reuses nothing, but a closed pane must not
    silently turn into keys for whatever comes next."""
    try:
        if tmux_src.pane_pid(agent.pane_id) != agent.pane_pid:
            raise Refused("that pane now runs something else")
    except SourceError:
        raise Refused("that pane is gone") from None


def instruct(agent, text):
    """Deliver the owner's words as the owner's own input. Never as a teammate message."""
    text = text.strip()
    if not text:
        raise Refused("the instruction is empty")
    _check_log_writable()
    if agent.pane_id:
        _same_pane(agent)
        try:
            label = auth_readers.detect_screen(tmux_src.capture(agent.pane_id))
        except SourceError:
            raise Refused("that pane could not be read") from None
        if label:
            _log(agent, "refused", "auth prompt on screen", request_text=label)
            raise Refused(f"that pane is asking for a credential ({label}). Open the pane and type it there")
        try:
            tmux_src.send_text(agent.pane_id, text)
        except SourceError as exc:
            _log(agent, "instruct", "error", instruction=text, error=str(exc))
            raise Refused(str(exc)) from None
        _log(agent, "instruct", "typed into pane", instruction=text)
        return "typed into its pane"
    if agent.kind == "codex":
        binary = os.environ.get("CODEX_BIN") or "codex"
        try:
            done = subprocess.run([binary, "queue", "--thread", agent.session_id, "--message", text],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(agent, "instruct", "error", instruction=text, error=type(exc).__name__)
            raise Refused(f"codex queue failed: {type(exc).__name__}") from None
        if done.returncode != 0:
            _log(agent, "instruct", "error", instruction=text, error=done.stderr.strip()[-200:])
            raise Refused("codex queue failed")
        _log(agent, "instruct", "queued", instruction=text)
        return "queued for it"
    raise Refused("a Claude session with no pane takes input only in its own terminal. Use Attach")


def open_pane(agent):
    if not agent.pane_id:
        raise Refused("this agent has no pane")
    _check_log_writable()
    _same_pane(agent)
    try:
        tmux_src.focus(agent.pane_id)
    except SourceError as exc:
        raise Refused(str(exc)) from None
    _log(agent, "open_pane", "focused", request_text=agent.auth or None)
    return "pane focused"


def attach(agent):
    if agent.kind != "claude" or not agent.short_id:
        raise Refused("only a background Claude session can be attached")
    _check_log_writable()
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        tmux_src.new_window(agent.name[:20], f"{binary} attach {agent.short_id}")
    except SourceError as exc:
        raise Refused(str(exc)) from None
    _log(agent, "attach", "opened in a new window")
    return "opened in a new window"


def retire(agent):
    if not agent.spawned:
        raise Refused("only an agent that spawn-peer created can be retired from here")
    _check_log_writable()
    agent_id = agent.session_id if agent.kind == "codex" else agent.short_id
    done = subprocess.run([os.path.join(ROOT, "bin", "retire-peer"), agent_id, "owner/agent-menu"],
                          capture_output=True, text=True, timeout=150)
    if done.returncode != 0:
        _log(agent, "retire", "error", error=done.stderr.strip()[-200:])
        raise Refused(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "retire-peer failed")
    _log(agent, "retire", "retired")
    return "retired"
