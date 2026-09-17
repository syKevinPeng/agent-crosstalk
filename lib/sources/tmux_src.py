"""tmux: list panes, read a pane, type into a pane, focus a pane, open a popup or window."""
import os
import subprocess

from .base import SourceError

FIELDS = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_command}\t#{pane_title}"


def _tmux(*args, timeout=1.5):
    binary = os.environ.get("TMUX_BIN") or "tmux"
    try:
        out = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"tmux: {type(exc).__name__}") from None
    if out.returncode != 0:
        raise SourceError(f"tmux {args[0]}: {out.stderr.strip()[:80] or 'failed'}")
    return out.stdout


def literal(text):
    """Make `text` safe as a tmux argument that tmux would otherwise interpret. tmux expands
    `#{...}` and runs `#(shell command)` in titles and window names, and a trailing `;` ends the
    command. Names come from other agents, so they are never passed through unescaped."""
    text = text.replace("#", "##")
    return text[:-1] + "\\;" if text.endswith(";") else text


def list_panes():
    """One dict per pane. Grouped sessions list each pane twice, so panes are keyed by `%id`."""
    panes = {}
    for line in _tmux("list-panes", "-a", "-F", FIELDS).splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or parts[0] in panes:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        panes[parts[0]] = {"pane_id": parts[0], "target": parts[1], "pid": pid,
                           "command": parts[3], "title": parts[4]}
    return list(panes.values())


def pane_pid(pane_id):
    text = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").strip()
    try:
        return int(text)
    except ValueError:
        raise SourceError("tmux: pane is gone") from None


def capture(pane_id):
    return _tmux("capture-pane", "-p", "-t", pane_id)


def send_text(pane_id, text):
    """Type `text` literally, then press Enter. `-l` stops tmux reading words as key names."""
    # -l types the text literally, with no key names and no formats. Only a trailing `;` still
    # needs care: tmux would take it as a command separator and drop it.
    _tmux("send-keys", "-t", pane_id, "-l", "--", text[:-1] + "\\;" if text.endswith(";") else text)
    _tmux("send-keys", "-t", pane_id, "Enter")


def focus(pane_id):
    _tmux("select-window", "-t", pane_id)
    _tmux("select-pane", "-t", pane_id)


def popup(command, width=100, height=26, title=""):
    """tmux draws the border and the title, so the program inside draws none of its own."""
    try:
        client = _tmux("display-message", "-p", "#{client_width} #{client_height}").split()
        width, height = min(width, int(client[0]) - 2), min(height, int(client[1]) - 2)
    except (SourceError, ValueError, IndexError):
        pass  # keep the wanted size; tmux says so if it cannot fit
    args = ["display-popup", "-E", "-w", str(max(width, 20)), "-h", str(max(height, 8)), "-b", "rounded"]
    if title:
        args += ["-T", literal(title)]
    _tmux(*args, command, timeout=None)


def new_window(name, command):
    _tmux("new-window", "-n", literal(name), command)


def descendants(root_pid):
    """Process ids below `root_pid`, read from /proc. Used to find which pane runs a session."""
    proc = os.environ.get("AGENT_MENU_PROC_ROOT") or "/proc"
    children = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return set()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "stat"), encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(entry))
    found, stack = set(), [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found
