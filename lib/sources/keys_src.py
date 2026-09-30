"""The tmux keys worth remembering: split a pane, zoom it, change and save the layout.

Read from the running server with `tmux list-keys`, never written down here, so the KEYS block
shows the bindings you actually have: plain tmux, byobu's function keys, or tmux-resurrect's.
Read-only, and it starts no process but tmux.
"""
import re

from . import tmux_src

# The actions the block reminds you of, in the order it lists them.
ACTIONS = ("sidebar", "split-side", "split-stacked", "zoom", "next-layout", "save-layout", "restore-layout")
BINDING = re.compile(r"^bind-key\s+(?:-r\s+)?-T\s+(\S+)\s+(\S+)\s+(.+)$")
# One command of several joined with `\;`, as list-keys prints them.
SEPARATOR = re.compile(r"\s+\\;\s+")


def _key_name(token):
    """list-keys escapes some keys (`\\%`, `\\"`) and quotes others (`"M-{"`)."""
    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token[1:] if len(token) == 2 and token[0] == "\\" else token


def action_of(command):
    """Which of ACTIONS a bound command does, or "". A menu that merely offers a split, like the
    default pane menu, is not a split: only a command that runs one counts. A split wrapped in
    `if-shell` or braces is not looked into, so it shows no row rather than a guessed one."""
    for part in SEPARATOR.split(command.strip()):
        words = part.split()
        if not words:
            continue
        verb, flags = words[0], words[1:]
        if verb == "run-shell" and "agent-menu-toggle" in part:
            return "sidebar"
        if verb == "split-window":
            # list-keys merges flags that take no value, so `-h -b` comes back as `-bh`.
            side = any(f.startswith("-") and not f.startswith("--") and "h" in f[1:] for f in flags)
            return "split-side" if side else "split-stacked"
        if verb == "resize-pane" and "-Z" in flags:
            return "zoom"
        if verb == "next-layout":
            return "next-layout"
        if verb == "run-shell" and "resurrect" in part:
            if "save.sh" in part:
                return "save-layout"
            if "restore.sh" in part:
                return "restore-layout"
        if "byobu-layout save" in part:     # byobu asks for a name first
            return "save-named-layout"
        if "byobu-layout restore" in part:  # and restores in a new window, asking which layout
            return "restore-named-layout"
    return ""


def parse(listing, prefix=""):
    """{action: [key, ...]} from `list-keys` output. A key in the root table is pressed alone and is
    listed first. A prefix key is written with the prefix in front, "C-a %", as you press it. The
    shortest key of each table wins, so the block shows the quickest way, not every way."""
    found = {}
    for line in listing.splitlines():
        match = BINDING.match(line)
        if not match:
            continue
        table, key, command = match.group(1), _key_name(match.group(2)), match.group(3)
        if table not in ("root", "prefix"):
            continue
        action = action_of(command)
        if action:
            found.setdefault(action, {}).setdefault(table, []).append(key)
    out = {action: _quickest(found[action], prefix) for action in ACTIONS if action in found}
    # tmux-resurrect saves and restores the whole session. Byobu's own save is a different store,
    # which its restore keys do not read, so it is shown only when resurrect is not bound at all.
    if "save-layout" not in out and "restore-layout" not in out:
        for named, action in (("save-named-layout", "save-layout"), ("restore-named-layout", "restore-layout")):
            if found.get(named):
                out[action] = _quickest(found[named], prefix)
    return out


def _quickest(tables, prefix):
    """The shortest root key, then the shortest prefix key written as you press it."""
    keys = [min(tables["root"], key=len)] if tables.get("root") else []
    if tables.get("prefix"):
        keys.append(f"{prefix or 'prefix'} {min(tables['prefix'], key=len)}")
    return keys


def read():
    """(prefix, {action: [key, ...]}) from the running tmux server. Raises SourceError."""
    prefix = tmux_src.global_option("prefix")
    listing = tmux_src.list_keys("root") + tmux_src.list_keys("prefix")
    return prefix, parse(listing, prefix)
