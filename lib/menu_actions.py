"""What the owner can do from the menu: instruct, open a pane, attach, retire.

Each action writes an `attempt` line to the action log BEFORE it acts, and is refused if that line
cannot be written. The result line follows. So an action can never happen without a record, even if
the program dies halfway. None of the actions has a command-line form. That is a guard against
accidents, not a security boundary: any program running as the owner can import this module, just as
it can run `tmux send-keys` itself."""
import datetime
import fcntl
import json
import os
import re
import shlex
import subprocess

import agent_state
import auth_readers
from sources import tmux_src
from sources.base import SourceError

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


class Refused(Exception):
    """The action was not performed. The message says why and is shown to the owner."""


def log_path():
    return os.environ.get("AGENT_MENU_ACTIONS_LOG") or os.path.join(ROOT, "log", "menu-actions.jsonl")


def _attempt(agent, action, **extra):
    """Record the intent first. If that fails, the action does not happen."""
    try:
        os.makedirs(os.path.dirname(log_path()) or ".", exist_ok=True)
        _log(agent, action, "attempt", **extra)
    except OSError:
        raise Refused("the action log cannot be written, so nothing was done") from None


def _result(agent, action, result, **extra):
    """Record the outcome. The attempt line already exists, so a failure here loses detail, not the record."""
    try:
        _log(agent, action, result, **extra)
    except OSError:
        pass


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
    """The pane must still hold the same first process AND still be running the agent's CLI in the
    foreground. When a CLI exits, its shell keeps the pane and the pid, so the pid alone proves nothing."""
    try:
        now = {p["pane_id"]: p for p in tmux_src.list_panes()}.get(agent.pane_id)
    except SourceError:
        raise Refused("that pane is gone") from None
    if not now:
        raise Refused("that pane is gone")
    if now["pid"] != agent.pane_pid or now["command"] != agent.kind:
        raise Refused("that pane now runs something else")
    if agent.name not in agent_state.title_names(agent.kind, now["title"]):
        # Same shell, same CLI, but the title names another session: the owner quit one and started another.
        raise Refused("that pane now shows a different session")


def instruct(agent, text):
    """Deliver the owner's words as the owner's own input. Never as a teammate message."""
    text = text.strip()
    if not text:
        raise Refused("the instruction is empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise Refused("an instruction is one line of plain text: no line breaks or control characters")
    if agent.pane_id:
        _same_pane(agent)
        try:
            label = auth_readers.detect_screen(tmux_src.capture(agent.pane_id))
        except SourceError:
            raise Refused("that pane could not be read") from None
        if label:
            _result(agent, "refused", "auth prompt on screen", request_text=label)
            raise Refused(f"that pane is asking for a credential ({label}). Open the pane and type it there")
        _attempt(agent, "instruct", instruction=text)
        try:
            tmux_src.send_text(agent.pane_id, text)
        except SourceError as exc:
            _result(agent, "instruct", "error", error=str(exc))
            raise Refused(str(exc)) from None
        _result(agent, "instruct", "typed into pane")
        return "typed into its pane"
    if agent.kind == "codex":
        _attempt(agent, "instruct", instruction=text)
        binary = os.environ.get("CODEX_BIN") or "codex"
        try:
            done = subprocess.run([binary, "queue", "--thread", agent.session_id, "--message", text],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _result(agent, "instruct", "error", error=type(exc).__name__)
            raise Refused(f"codex queue failed: {type(exc).__name__}") from None
        if done.returncode != 0:
            _result(agent, "instruct", "error", error=done.stderr.strip()[-200:])
            raise Refused("codex queue failed")
        _result(agent, "instruct", "queued")
        return "queued for it"
    raise Refused("a Claude session with no pane takes input only in its own terminal. Use Attach")


def open_pane(agent):
    if not agent.pane_id:
        raise Refused("this agent has no pane")
    _same_pane(agent)
    _attempt(agent, "open_pane", request_text=agent.auth or None)
    try:
        tmux_src.focus(agent.pane_id)
    except SourceError as exc:
        _result(agent, "open_pane", "error", error=str(exc))
        raise Refused(str(exc)) from None
    _result(agent, "open_pane", "focused")
    return "pane focused"


def attach(agent):
    if agent.kind != "claude" or not agent.background or agent.pane_id:
        raise Refused("only a background Claude session with no pane can be attached")
    if not re.fullmatch(r"[0-9a-f]{6,32}", agent.short_id or ""):
        raise Refused("this session has no usable id")  # the id goes into a command line
    _attempt(agent, "attach")
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        tmux_src.new_window(agent.name[:20], f"{shlex.quote(binary)} attach {agent.short_id}")
    except SourceError as exc:
        _result(agent, "attach", "error", error=str(exc))
        raise Refused(str(exc)) from None
    _result(agent, "attach", "opened in a new window")
    return "opened in a new window"


def retire(agent):
    if not agent.spawned or agent.retired:
        raise Refused("only a live agent that spawn-peer created can be retired from here")
    _attempt(agent, "retire")
    agent_id = agent.session_id if agent.kind == "codex" else agent.short_id
    try:
        done = subprocess.run([os.path.join(ROOT, "bin", "retire-peer"), agent_id, "owner/agent-menu"],
                              capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _result(agent, "retire", "error", error=type(exc).__name__)
        raise Refused(f"retire-peer failed: {type(exc).__name__}") from None
    if done.returncode != 0:
        _result(agent, "retire", "error", error=done.stderr.strip()[-200:])
        raise Refused(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "retire-peer failed")
    _result(agent, "retire", "retired")
    return "retired"


def perform(agent, button, text=""):
    """Run one popup button. Returns (note for the owner, succeeded?, close the popup?).
    Nothing that goes wrong here may crash the popup: the owner is told instead."""
    actions = {"Open pane": (open_pane, True), "Attach": (attach, True), "Retire": (retire, False)}
    if button in ("Send", "Retire") and os.environ.get("AGENT_MENU_LOOK_ONLY"):
        return "not done: look-only mode", False, False      # refused here too, not only hidden in the popup
    try:
        if button == "Send":
            return instruct(agent, text), True, False
        if button in actions:
            action, close = actions[button]
            return action(agent), True, close
    except Refused as refusal:
        return f"not done: {refusal}", False, False
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not done: {type(exc).__name__}", False, False
    return "", False, False
