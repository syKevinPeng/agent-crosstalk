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
import codex_delivery
from codex_ws import CodexError, CodexWS
import menu_detail
from sources import claude_src, tmux_src
from sources.base import SourceError, clean
import spawn_log

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
    foreground. When a CLI exits, its shell keeps the pane and the pid, so the pid alone proves nothing.
    Returns the pane as tmux lists it now."""
    try:
        panes = tmux_src.list_panes()
    except SourceError:
        raise Refused("that pane is gone") from None
    now = {p["pane_id"]: p for p in panes}.get(agent.pane_id)
    if not now:
        raise Refused("that pane is gone")
    if now["pid"] != agent.pane_pid or now["command"] != agent.kind:
        raise Refused("that pane now runs something else")
    if agent.kind == "claude":
        # Checked the way it was matched: by process. Claude Code rewrites its title with live status,
        # so the title may not name the session at all.
        if not tmux_src.alive(agent.pid):
            raise Refused("that session's process is gone")
        owner = agent_state.pane_for_pid(agent.pid, panes)
        if not owner or owner["pane_id"] != agent.pane_id:
            raise Refused("that session no longer runs in that pane")
        # Still under the pane is not enough: stopped with Ctrl+Z while another session runs in the
        # same shell, it stays under the pane but is not what the pane shows. The terminal's foreground
        # group says which one is. Only when /proc cannot tell does the title decide.
        shown = tmux_src.in_foreground(agent.pid)
        if shown is False or (shown is None and agent.name not in agent_state.title_names(agent.kind, now["title"])):
            raise Refused("that pane now shows a different session")
    elif agent.name not in agent_state.title_names(agent.kind, now["title"]):
        # Same shell, same CLI, but the title names another session: the owner quit one and started another.
        raise Refused("that pane now shows a different session")
    return now


# How long a Send to a Codex agent with no pane waits for a turn to take the text. The popup waits too.
DELIVERY_WAIT = 3.0
DELIVERY_NOTES = {
    "started": "queued, and a turn took it",
    "waiting": "queued: it takes it when its current turn ends",
    "not-loaded": "queued, but it is not loaded, so nothing runs it until it is opened. Not woken: {note}",
    "stuck": "queued, but no turn took it yet. Check its latest answer before sending again",
    "unknown": "queued, but whether a turn takes it could not be checked ({note})",
}


def instruct(agent, text):
    """Deliver the owner's words as the owner's own input. Never as a teammate message."""
    text = text.strip()
    if not text:
        raise Refused("the instruction is empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise Refused("an instruction is one line of plain text: no line breaks or control characters")
    if agent.pane_id:
        pane = _same_pane(agent)
        if pane["in_mode"]:
            # Copy mode (entered by scrolling back) takes the keys itself: the text would be lost, and
            # a letter such as f or t would leave a jump prompt open on the owner's screen.
            raise Refused("that pane is in copy mode. Leave it (q) and try again")
        if pane["synchronized"]:
            raise Refused("that window has synchronize-panes on, so the text would go to every pane in it")
        try:
            screen = tmux_src.capture(agent.pane_id)
        except SourceError:
            raise Refused("that pane could not be read") from None
        label = auth_readers.detect_screen(screen)
        if label:
            _result(agent, "refused", "auth prompt on screen", request_text=label)
            raise Refused(f"that pane is asking for a credential ({label}). Open the pane and type it there")
        # Checked on the pane as it is now: the popup's reading can be older than the prompt.
        if agent.needs_owner or agent_state.waits_on_owner(pane["title"], screen):
            _result(agent, "refused", "waiting for the owner")
            raise Refused("that pane is waiting for your answer, and typed text would answer its prompt. "
                          "Open the pane and answer it there")
        _attempt(agent, "instruct", instruction=text)
        try:
            tmux_src.send_text(agent.pane_id, text)
        except tmux_src.EnterFailed as exc:
            # The text is in the pane. Reporting a failure would invite a second send, which doubles it.
            _result(agent, "instruct", "typed, Enter failed", error=str(exc))
            return "typed into its pane, but Enter failed: press Enter there, do not send it again"
        except SourceError as exc:
            _result(agent, "instruct", "error", error=str(exc))
            raise Refused(str(exc)) from None
        _result(agent, "instruct", "typed into pane")
        return "typed into its pane"
    if agent.kind == "codex":
        _attempt(agent, "instruct", instruction=text)
        # `codex queue` only stores the text. The daemon runs it only on a loaded thread, so an agent
        # it unloaded is woken first, the same way and with the same limits as bin/send-to-codex.
        wake_error = None
        try:
            woke = codex_delivery.prepare(agent.session_id)["woke"]
        except Exception as exc:  # noqa: BLE001 -- the wake is a help, never a reason not to send
            woke, wake_error = False, f"{type(exc).__name__}: {exc}"[:200]
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
        found = re.search(r"Queued message (\S+) for thread (\S+?)\.?\s*$", done.stdout.strip())
        outcome = {"delivery": "unknown", "woke": woke, "note": "codex did not print the queued id"}
        if found:
            try:
                outcome = codex_delivery.confirm(found.group(2), found.group(1), woke=woke, wait=DELIVERY_WAIT,
                                                 marker=text)
            except Exception as exc:  # noqa: BLE001 -- the text is queued: report that, or it gets sent twice
                outcome = {"delivery": "unknown", "woke": woke, "note": f"{type(exc).__name__}: {exc}"[:200]}
        if wake_error:
            outcome["wake_error"] = wake_error
        _result(agent, "instruct", "queued", **outcome)
        note = DELIVERY_NOTES[outcome["delivery"]].format(note=outcome.get("note") or "no reason given")
        if wake_error and outcome["delivery"] not in codex_delivery.DELIVERED:
            note += f". The wake before it failed: {wake_error}"
        return note
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
    # A stopped session is offered a record-only Quit. If it came back since the popup read it,
    # retire-peer refuses instead of stopping it behind a confirm that promised a record only.
    expect = ["--expect-stopped"] if agent.quit else []
    try:
        done = subprocess.run([os.path.join(ROOT, "bin", "retire-peer"), *expect, agent_id, "owner/agent-menu"],
                              capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _result(agent, "retire", "error", error=type(exc).__name__)
        raise Refused(f"retire-peer failed: {type(exc).__name__}") from None
    if done.returncode != 0:
        _result(agent, "retire", "error", error=done.stderr.strip()[-200:])
        raise Refused(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "retire-peer failed")
    _result(agent, "retire", "retired")
    return "retired"


SHORT_ID = re.compile(r"[0-9a-f]{6,32}")
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _codex_rpc(method, params):
    ws = CodexWS()
    try:
        return ws.rpc(method, params)
    finally:
        ws.close()


def _check_codex(agent):
    """The id must still belong to a thread of this name. Refuse if the daemon says otherwise."""
    if not THREAD_ID.fullmatch(agent.session_id or ""):
        raise Refused("this thread has no usable id")
    try:
        thread = _codex_rpc("thread/read", {"threadId": agent.session_id}).get("thread")
    except CodexError as exc:
        raise Refused(f"codex: {exc}") from None
    name = (clean(thread.get("name")).strip() or agent.session_id[:8]) if isinstance(thread, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != agent.session_id or name != agent.name:
        raise Refused("that id no longer belongs to a thread of that name")


def _claude_session(agent, include_quit):
    for session in claude_src.list_sessions(include_quit=include_quit):
        if session["session_id"] == agent.session_id:
            return session
    return None


def quit_agent(agent):
    """Stop or archive an agent so it can be resumed later. Nothing is deleted."""
    if agent.quit and not menu_detail.can_quit(agent):
        raise Refused("that agent is already quit")
    if agent.pane_id or agent.maybe_in_pane:
        raise Refused("it runs or may run in a pane, so quit it there")
    if agent.spawned:
        retire(agent)                              # spawn-peer's own path, so the spawn record notes it
        return "quit. Resume brings it back"
    if agent.kind == "codex":
        _check_codex(agent)
        _attempt(agent, "quit")
        try:
            _codex_rpc("thread/archive", {"threadId": agent.session_id})
        except CodexError as exc:
            _result(agent, "quit", "error", error=str(exc)[:200])
            raise Refused(f"archive failed: {exc}") from None
        _result(agent, "quit", "archived")
        return "quit: archived. Resume brings it back"
    if not agent.background or not SHORT_ID.fullmatch(agent.short_id or ""):
        raise Refused("an interactive Claude session ends only when its own terminal is closed")
    try:
        session = _claude_session(agent, include_quit=False)
    except SourceError as exc:
        raise Refused(str(exc)) from None
    if not session or session["name"] != agent.name or session["short_id"] != agent.short_id:
        raise Refused("that session is no longer running under that name")
    _attempt(agent, "quit")
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        done = subprocess.run([binary, "stop", agent.short_id], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _result(agent, "quit", "error", error=type(exc).__name__)
        raise Refused(f"claude stop failed: {type(exc).__name__}") from None
    if done.returncode != 0:
        _result(agent, "quit", "error", error=done.stderr.strip()[-200:])
        raise Refused("claude stop failed")
    _result(agent, "quit", "stopped")
    return "quit: stopped. Resume brings it back"


def _note_resumed(agent):
    """For an agent spawn-peer created, record the resume in its spawn record, or the menu and
    retire-peer would go on treating it as retired. Returns an extra note for the owner."""
    if not agent.spawned:
        return ""
    try:
        spawn_log.append({"time_utc": spawn_log.now(), "event": "resumed", "kind": agent.kind,
                          "id": agent.session_id if agent.kind == "codex" else agent.short_id,
                          "resumed_by": "owner/agent-menu"})
    except OSError:
        return " (the spawn record could not be updated, so it may still show as retired)"
    return ""


def resume(agent):
    """Bring a quit agent back and open it in a new window, right after the current one."""
    if not agent.quit:
        raise Refused("that agent is not quit")
    if agent.kind == "codex":
        _check_codex(agent)
        _attempt(agent, "resume")
        try:
            _codex_rpc("thread/unarchive", {"threadId": agent.session_id})
        except CodexError as exc:
            _result(agent, "resume", "error", error=str(exc)[:200])
            raise Refused(f"unarchive failed: {exc}") from None
        note = _note_resumed(agent)          # it is live from here on, whether or not its window opens
        binary = os.environ.get("CODEX_BIN") or "codex"
        try:
            tmux_src.new_window(agent.name[:20], f"{shlex.quote(binary)} resume {agent.session_id}", cwd=agent.cwd)
        except SourceError as exc:
            _result(agent, "resume", "unarchived, window failed", error=str(exc))
            raise Refused(f"unarchived, but its window could not open: {exc}{note}") from None
        _result(agent, "resume", "unarchived and opened")
        return "resumed in a new window" + note
    if not SHORT_ID.fullmatch(agent.short_id or ""):
        raise Refused("this session has no usable id")
    try:
        session = _claude_session(agent, include_quit=True)
    except SourceError as exc:
        raise Refused(str(exc)) from None
    if not session or not session["quit"] or session["name"] != agent.name:
        raise Refused("that session is no longer stopped under that name")
    _attempt(agent, "resume")
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        tmux_src.new_window(agent.name[:20], f"{shlex.quote(binary)} attach {agent.short_id}")
    except SourceError as exc:
        _result(agent, "resume", "error", error=str(exc))
        raise Refused(str(exc)) from None
    _result(agent, "resume", "attached in a new window")
    return "resumed in a new window" + _note_resumed(agent)


def perform(agent, button, text=""):
    """Run one popup button. Returns (note for the owner, succeeded?, close the popup?).
    Nothing that goes wrong here may crash the popup: the owner is told instead."""
    actions = {"Open pane": (open_pane, True), "Attach": (attach, True),
               "Quit agent": (quit_agent, False), "Resume": (resume, True)}
    if button in ("Send", "Quit agent") and os.environ.get("AGENT_MENU_LOOK_ONLY"):
        return "not done: look-only mode", False, False      # refused here too, not only hidden in the popup
    if button not in ("Send", *actions):
        return "", False, False
    if button not in menu_detail.buttons(agent):
        # The popup's own rule, checked again here: a reload may have withdrawn the button (the agent
        # was retired, or now waits on a prompt) while a key for it was already on its way.
        return "not done: that is no longer offered for this agent", False, False
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
