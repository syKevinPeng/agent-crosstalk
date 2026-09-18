"""tmux: list panes, read a pane, type into a pane, focus a pane, open a popup or window."""
import os
import subprocess
import time

from .base import SourceError

# The title comes last and may itself hold tabs, so a line is split at most len(FIELDS) - 1 times.
FIELDS = ("#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_current_command}"
          "\t#{pane_in_mode}\t#{pane_synchronized}\t#{pane_title}")
# Codex takes keys that arrive in a fast burst as a paste, and an Enter within 120 ms of that burst as
# a newline inside it (codex-rs tui paste_burst.rs, PASTE_ENTER_SUPPRESS_WINDOW). send-keys types the
# text as exactly such a burst, so the Enter waits until the burst is over, or it would not submit.
ENTER_DELAY = 0.3


class EnterFailed(SourceError):
    """The text was typed, but the Enter after it failed. Typing it again would double it."""


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
        parts = line.split("\t", 6)
        if len(parts) != 7 or parts[0] in panes:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        # in_mode: copy or view mode, where keys drive the mode and never reach the program.
        # synchronized: keys typed into the pane go to every pane of its window.
        panes[parts[0]] = {"pane_id": parts[0], "target": parts[1], "pid": pid, "command": parts[3],
                           "in_mode": parts[4] == "1", "synchronized": parts[5] == "1", "title": parts[6]}
    return list(panes.values())


def pane_pid(pane_id):
    text = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").strip()
    try:
        return int(text)
    except ValueError:
        raise SourceError("tmux: pane is gone") from None


def capture(pane_id):
    """The visible screen. -J joins lines the pane wrapped, so a prompt wider than the pane is read
    as the one line it is, and a pattern anchored to its line still finds it."""
    return _tmux("capture-pane", "-p", "-J", "-t", pane_id)


def send_text(pane_id, text):
    """Type `text` literally, then press Enter once the typing has settled (see ENTER_DELAY).
    `-l` stops tmux reading words as key names. Raises EnterFailed when only the Enter failed."""
    # -l types the text literally, with no key names and no formats. Only a trailing `;` still
    # needs care: tmux would take it as a command separator and drop it.
    _tmux("send-keys", "-t", pane_id, "-l", "--", text[:-1] + "\\;" if text.endswith(";") else text)
    time.sleep(ENTER_DELAY)
    try:
        _tmux("send-keys", "-t", pane_id, "Enter")
    except SourceError as exc:
        raise EnterFailed(str(exc)) from None


def resize(pane_id, columns):
    _tmux("resize-pane", "-t", pane_id, "-x", str(int(columns)))


def focus(pane_id):
    """Bring the pane to the owner. select-window and select-pane change only the pane's own session,
    so the client is first switched to that session, which matters when the pane lives in another."""
    try:
        _tmux("switch-client", "-t", pane_id)
    except SourceError:
        pass  # no client to switch, as when called from outside tmux: selecting still works
    _tmux("select-window", "-t", pane_id)
    _tmux("select-pane", "-t", pane_id)


def option(target, name, scope="p", inherited=False):
    """The value a pane (scope p) or window (scope w) sets itself for option `name`, "" when it sets
    none. With `inherited`, the value in effect there, wherever it is set."""
    return _tmux("show-options", f"-{scope}", "-q", "-v", *(["-A"] if inherited else []),
                 "-t", target, name).rstrip("\n")


def set_option(target, name, value=None, scope="p"):
    """Set an option on a pane or window itself, or with `value` None remove it there."""
    if value is None:
        _tmux("set-option", f"-{scope}", "-u", "-t", target, name)
    else:
        _tmux("set-option", f"-{scope}", "-t", target, name, value)


def window_of(pane_id):
    return _tmux("display-message", "-p", "-t", pane_id, "#{window_id}").strip()


def panes_with_option(name, window=None):
    """The ids of the panes, in one window or in all, that set the user option `name` themselves."""
    where = ["-t", window] if window else ["-a"]
    found = []
    for line in _tmux("list-panes", *where, "-F", f"#{{pane_id}}\t#{{{name}}}").splitlines():
        pane, _, value = line.partition("\t")
        if pane.startswith("%") and value and pane not in found:
            found.append(pane)
    return found


def windows_with_option(name):
    found = []
    for line in _tmux("list-windows", "-a", "-F", f"#{{window_id}}\t#{{{name}}}").splitlines():
        window, _, value = line.partition("\t")
        if window.startswith("@") and value and window not in found:
            found.append(window)
    return found


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


def new_window(name, command, cwd=None):
    """Open right after the current window. Without -a tmux takes the first free number, which can
    put the new window ahead of the one the owner works in and reorder their tabs. A folder is used
    only when it exists and holds no `#`: tmux reads formats in it, and a doubled `#` would name the
    wrong folder, so such a folder is simply not passed."""
    args = ["new-window", "-a", "-n", literal(name)]
    if cwd and "#" not in cwd and os.path.isdir(cwd):
        args += ["-c", cwd]
    _tmux(*args, command)


def alive(pid):
    proc = os.environ.get("AGENT_MENU_PROC_ROOT") or "/proc"
    return bool(pid) and os.path.isdir(os.path.join(proc, str(pid)))


def in_foreground(pid):
    """Whether `pid`'s process group is the foreground group of its terminal, which is what a pane
    shows: True or False, or None when /proc cannot say (no terminal, no such process)."""
    proc = os.environ.get("AGENT_MENU_PROC_ROOT") or "/proc"
    try:
        with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8", errors="replace") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        pgrp, tpgid = int(fields[2]), int(fields[5])     # after the name: state ppid pgrp session tty tpgid
    except (OSError, ValueError, IndexError):
        return None
    return None if pgrp <= 0 or tpgid <= 0 else pgrp == tpgid


def ancestors(pid, limit=40):
    """[(pid, command), ...] from `pid` up towards init, itself first."""
    proc = os.environ.get("AGENT_MENU_PROC_ROOT") or "/proc"
    chain, seen = [], set()
    while pid and pid > 1 and pid not in seen and len(chain) < limit:
        seen.add(pid)
        try:
            with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            command = stat[stat.index("(") + 1:stat.rindex(")")]
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
        chain.append((pid, command))
        pid = parent
    return chain


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
