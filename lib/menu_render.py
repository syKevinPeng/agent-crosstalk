"""Turn a Snapshot into sidebar rows. Pure functions: no curses, no I/O."""
import os
import time
import unicodedata
from dataclasses import replace as dataclass_replace

import menu_style

MIN_COLUMNS, MIN_ROWS = 20, 5
# How wide the pane must be for each layout. Narrow drops the kind and access columns; wide adds
# the working folder and the model. `w` resizes the pane between STRIP_WIDTH and PANEL_WIDTH.
NORMAL_COLUMNS, WIDE_COLUMNS = 26, 48
STRIP_WIDTH, PANEL_WIDTH = 20, 62
FOLDER_COLUMNS, MODEL_COLUMNS = 16, 11
LOUD_PERCENT = 90        # at or above this a limit bar turns red, reusing the "needs you" colour
LIMITS_MIN_COLUMNS = 26  # narrower than this the block cannot say anything useful, so the tree keeps the space
STALE_SECONDS = 45 * 60  # a reading older than this is marked, never quietly shown as current
HINT_FULL = " ⏎ open  ←→ fold  w wide  u limits  ? help"
HINT_SHORT = " ⏎ open  u limits  ? help"
HINT_ASCII = " Enter open  u limits  ? help"
# Every help line fits the narrowest sidebar (32 cells) and the list fits 12 rows, so nothing is cut.
HELP = ["KEYS  (mouse: click to open)", "↑ ↓ j k    move", "→ l open   ← h fold or parent",
        "Space fold  Home End g G jump", "Enter popup   a quit agents", "u limits   w width   q quit",
        "MARKS", "✗ login   !n prompt   ✉n unread", "⠋ working   ✓ idle   ? no state", "- stopped or quit",
        "", "Press any key."]
HELP_ASCII = ["KEYS  (mouse: click to open)", "up down j k  move", "right l open  left h fold/parent",
              "Space fold  Home End g G jump", "Enter popup   a quit agents", "u limits   w width   q quit",
              "MARKS", "X login   !n prompt   +n unread", "* working   . idle   ? no state", "- stopped or quit",
              "", "Press any key."]
MAX_INDENT = 4


def width(text):
    """Screen cells. A combining mark sits on the character before it and takes none."""
    return sum(0 if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def fit(text, columns, ellipsis="…"):
    """Cut `text` to `columns` screen cells, ending in the ellipsis when cut."""
    if width(text) <= columns:
        return text
    out = ""
    for ch in text:
        if width(out + ch) > max(columns - width(ellipsis), 0):
            break
        out += ch
    return out + ellipsis


def short_reset(seconds):
    """Seconds until a reset, short enough for a narrow column."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "now"
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{suffix}"
    return "now"


def tier_for(columns):
    if columns >= WIDE_COLUMNS:
        return "wide"
    return "normal" if columns >= NORMAL_COLUMNS else "narrow"


def pad(text, columns):
    return text + " " * max(columns - width(text), 0)


def resize_target(columns):
    """What `w` resizes the pane to: a strip when it is already wide, a panel otherwise."""
    return STRIP_WIDTH if columns >= WIDE_COLUMNS else PANEL_WIDTH


def pad_left(text, columns):
    return " " * max(columns - width(text), 0) + text


def short_folder(path, columns, ellipsis="…"):
    """A folder that fits: the home folder becomes ~, then leading parts drop away."""
    home = os.path.expanduser("~")
    if path and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    if width(path) <= columns:
        return path
    parts = path.split("/")
    head, tail = parts[0], [part for part in parts[1:] if part]
    best = ""
    for keep in range(1, len(tail) + 1):        # keep the root and as much of the end as fits
        candidate = f"{head}/{ellipsis}/" + "/".join(tail[-keep:])
        if width(candidate) > columns:
            break
        best = candidate
    for keep in range(1, len(tail) + 1):        # no room for the root: keep only the end
        candidate = ellipsis + "/" + "/".join(tail[-keep:])
        if width(candidate) > columns:
            break
        best = best or candidate
    return best or fit(tail[-1] if tail else path, columns, ellipsis)


def status(agent):
    """The one state a row shows, most urgent first. A retired or quit agent shows nothing else."""
    if agent.retired or agent.parked:
        return "stopped"
    if agent.auth:
        return "auth"
    if agent.needs_owner:
        return "needs"
    if agent.open_messages:
        return "unanswered"
    if agent.state in ("working", "idle"):
        return agent.state
    if agent.state == "unknown" and not agent.quit:
        return "running"     # alive, but its CLI reports no state, as for a Claude session in a terminal
    return "stopped"


def activity(agent):
    """What the agent is doing, leaving aside prompts and unread messages."""
    return status(dataclass_replace(agent, auth="", needs_owner=0, open_messages=0))


def mark(agent, glyphs=None, frame=0):
    glyphs = glyphs or menu_style.UNICODE
    state = status(agent)
    if state == "needs":
        return f"{glyphs['needs']}{agent.needs_owner}"
    if state == "unanswered":
        return f"{glyphs['unanswered']}{agent.open_messages}"
    if state == "working":
        return glyphs["spinner"][frame % len(glyphs["spinner"])]
    return glyphs[state]


def header(snapshot, columns=34, glyphs=None):
    """The counts that matter, in words when they fit and as bare marks when they do not."""
    glyphs = glyphs or menu_style.UNICODE
    agents = [a for a in snapshot.by_key.values() if not a.parked]   # parked agents are not waiting
    counts = [(glyphs["auth"], sum(1 for a in agents if a.auth), "needs login"),
              (glyphs["needs"], sum(1 for a in agents if a.needs_owner and not a.auth), "need you"),
              (glyphs["unanswered"], sum(a.open_messages for a in agents), "unanswered")]
    counts = [c for c in counts if c[1]]
    if not counts:
        return " AGENTS  all quiet"
    wordy = " AGENTS  " + glyphs["separator"].join(f"{n} {words}" for _, n, words in counts)
    if width(wordy) <= columns:
        return wordy
    return " AGENTS  " + "  ".join(f"{symbol}{n}" for symbol, n, _ in counts)


def footer(columns, note="", glyphs=None):
    ascii_only = glyphs is menu_style.ASCII
    if note:
        return fit(" " + note, columns, "~" if ascii_only else "…")
    if ascii_only:
        return fit(HINT_ASCII, columns, "~")
    return HINT_FULL if width(HINT_FULL) <= columns else fit(HINT_SHORT, columns)


def kind_width(snapshot):
    """Width of the kind column, so `ro codex` and `claude` line up in wide mode."""
    widths = [width(f"{a.access + ' ' if a.access else ''}{a.kind}") for a in snapshot.by_key.values()]
    return max(widths + [width("retired")]) if widths else width("retired")


def rows(snapshot, columns, collapsed=(), glyphs=None, frame=0, tier=None):
    """[{text, key, style, mark, look}] for the sidebar. `text` always fits `columns` and ends with
    `mark`, so the drawing layer can colour the mark alone. style: header | error | row | retired."""
    glyphs = glyphs or menu_style.UNICODE
    tier = tier or tier_for(columns)
    kind_columns = kind_width(snapshot)
    mark_columns = max([width(mark(a, glyphs, frame)) for a in snapshot.by_key.values()] or [1])

    def spare(tier):
        """Columns left for indentation once a row holds its lead, fold, a 4-cell name, the gap, the
        columns on the right and the held-back last cell. Below zero the row cannot fit at all."""
        right = {"narrow": mark_columns, "normal": kind_columns + 1 + mark_columns,
                 "wide": kind_columns + 2 + FOLDER_COLUMNS + 2 + MODEL_COLUMNS + 1 + mark_columns}[tier]
        return columns - (1 + 2 + 4 + 1 + right + 1)
    if tier == "wide" and spare("wide") < 0:
        tier = "normal"          # no room for the folder and model: drop them, never the mark at the end
    indent_room = max(spare(tier) // 2, 0)
    out = [{"text": fit(header(snapshot, columns, glyphs), columns, glyphs["ellipsis"]),
            "key": "", "style": "header", "mark": "", "look": ""}]
    for error in snapshot.errors:
        out.append({"text": fit(f" {error}", columns, glyphs["ellipsis"]), "key": "", "style": "error",
                    "mark": "", "look": ""})

    def add(agent, prefix, fold):
        sign = pad_left(mark(agent, glyphs, frame), mark_columns)   # one column, so what follows lines up
        if tier == "narrow":
            right = sign                                   # a strip says who needs you, nothing else
        else:
            kind = "quit" if agent.parked else ("retired" if agent.retired else
                                              f"{agent.access + ' ' if agent.access else ''}{agent.kind}")
            right = f"{kind} {sign}"
            if tier == "wide":
                folder = pad(short_folder(agent.cwd, FOLDER_COLUMNS, glyphs["ellipsis"]), FOLDER_COLUMNS)
                model = pad(fit(agent.model, MODEL_COLUMNS, glyphs["ellipsis"]), MODEL_COLUMNS)
                right = f"{pad(kind, kind_columns)}  {folder}  {model} {sign}"
        room = columns - width(prefix) - width(fold) - width(right) - 2
        name = fit(agent.name, max(room, 4), glyphs["ellipsis"])
        gap = " " * max(columns - width(prefix) - width(fold) - width(name) - width(right) - 1, 1)
        style = "quit" if agent.parked else ("retired" if agent.retired else "row")
        out.append({"text": f"{prefix}{fold}{name}{gap}{right}", "key": agent.key,
                    "style": style, "mark": sign, "look": status(agent)})

    drawn = set()

    def walk(agent, depth, last):
        if agent.key in drawn:
            return  # a loop in the records must not draw, or recurse, forever
        drawn.add(agent.key)
        folded = agent.key in collapsed
        if depth == 0:
            add(agent, " ", (glyphs["closed"] if folded else glyphs["open"]) if agent.children else glyphs["leaf"])
        else:
            levels = min(depth, MAX_INDENT, max((columns - 22) // 2, 0), indent_room)   # a narrow pane indents less
            add(agent, " " + "  " * levels, glyphs["last"] if last else glyphs["branch"])
        if not folded:
            for index, child in enumerate(agent.children):
                walk(child, depth + 1, index == len(agent.children) - 1)

    for root in snapshot.roots:
        walk(root, 0, True)
    if snapshot.quit_agents:
        out.append({"text": fit(" QUIT" + glyphs["separator"] + "a hides", columns, glyphs["ellipsis"]),
                    "key": "", "style": "quitdiv", "mark": "", "look": ""})
        for agent in snapshot.quit_agents:
            add(agent, " ", glyphs["leaf"])
    return out


def pinned_count(rendered):
    """Rows that stay at the top while the list scrolls: the header and any error lines. Only the
    leading run counts, so a divider further down scrolls with the rows around it."""
    count = 0
    for row in rendered:
        if row["key"]:
            break
        count += 1
    return count


def scroll_top(rendered, selected, room, top):
    """First row to draw so that the selected row is on screen. Rows 0.. hold the header and any
    error lines, which stay pinned, so `room` is what is left under them for agent rows."""
    pinned = pinned_count(rendered)
    body = [row["key"] for row in rendered[pinned:]]
    if selected not in body or room <= 0:
        return 0
    index = body.index(selected)
    top = min(top, index)
    top = max(top, index - room + 1)
    return max(0, min(top, max(len(body) - room, 0)))


def bar(percent, width, glyphs):
    """A `width`-cell bar. Rounds down, so a bar is never full until the limit really is."""
    percent = max(0, min(100, int(percent)))
    filled = min(width, percent * width // 100)
    if percent and not filled:
        filled = 1        # some use must never look like none
    return glyphs["bar_full"] * filled + glyphs["bar_empty"] * (width - filled)


def _limit_groups(limits):
    """Parent rows (a whole window) each followed by their per-model rows."""
    parents = [x for x in limits if not x.get("model")]
    children = [x for x in limits if x.get("model")]
    taken, groups = set(), []
    for parent in parents:
        kids = [c for c in children
                if (c["source"], c["window"]) == (parent["source"], parent["window"])]
        taken.update(id(c) for c in kids)
        groups.append((parent, kids))
    for orphan in children:                       # a model with no total of its own still shows
        if id(orphan) not in taken:
            groups.append((orphan, []))
    return groups


def limit_rows(limits, columns, glyphs=None, taken_at=None, now=None, errors=()):
    """[{text, look}] for the LIMITS block. Pure: `now` and `taken_at` are seconds, passed in."""
    glyphs = glyphs or menu_style.UNICODE
    if (not limits and not errors) or columns < LIMITS_MIN_COLUMNS:
        return []
    out = [{"text": glyphs["rule"] * columns, "look": "faint"}]
    head, age = " LIMITS", ""
    if taken_at is not None and now is not None:
        stamp = time.strftime("%H:%M", time.localtime(taken_at))
        age = f"stale, {stamp} " if now - taken_at > STALE_SECONDS else f"as of {stamp} "
    gap = columns - width(head) - width(age)
    out.append({"text": head + " " * max(gap, 1) + age if gap >= 1 else fit(head, columns, glyphs["ellipsis"]),
                "look": "faint"})

    rows = []
    for parent, kids in _limit_groups(limits):
        label = f"{parent['source']} {parent['window']}".strip()
        rows.append(("  ", label, parent))
        for kid in kids:
            rows.append(("   " + glyphs["last"], kid["model"], kid))
    if not rows:
        for error in errors:
            out.append({"text": fit(" " + error, columns, glyphs["ellipsis"]), "look": "auth"})
        return out

    # Fit the columns: label, bar, percent, and the reset time if there is room left for it.
    label_room = max(columns - 2 - 1 - 4 - 1 - 4 - 4, 4)
    label_width = min(max(width(lead) + width(label) for lead, label, _ in rows), label_room)
    resets = [short_reset(row[2].get("resets_in")) for row in rows]
    reset_width = max((width(r) for r in resets), default=0)
    bar_width = columns - 2 - label_width - 1 - 4 - 1
    if bar_width - reset_width - 1 >= 3:
        bar_width -= reset_width + 1
    else:
        resets, reset_width = ["" for _ in rows], 0
    bar_width = max(bar_width, 3)

    for (lead, label, limit), reset in zip(rows, resets):
        name = fit(label, max(label_width - width(lead), 1), glyphs["ellipsis"])
        pad = " " * max(label_width - width(lead) - width(name), 0)
        tail = f"{min(int(limit['percent']), 999):3d}%" + (f" {reset:>{reset_width}}" if reset_width else "")
        head_text = f"{lead}{name}{pad} {bar(limit['percent'], bar_width, glyphs)}"
        spacer = " " * max(columns - width(head_text) - width(tail), 1)   # numbers line up on the right edge
        out.append({"text": fit(head_text + spacer + tail, columns, glyphs["ellipsis"]),
                    "look": "auth" if limit["percent"] >= LOUD_PERCENT else ""})
    for error in errors:
        out.append({"text": fit(" " + error, columns, glyphs["ellipsis"]), "look": "auth"})
    return out


def key_at(rendered, row):
    """The agent key on screen row `row`, or "" for the header, an error line or empty space."""
    return rendered[row]["key"] if 0 <= row < len(rendered) else ""


def move(snapshot, collapsed, selected, action):
    """Tree keys after the WAI-ARIA tree pattern. Returns (selected, collapsed)."""
    collapsed = set(collapsed)
    visible = [r["key"] for r in rows(snapshot, 200, collapsed) if r["key"]]
    if selected not in visible and action == "stay" and selected:
        # A row can vanish for one refresh, when a CLI is slow. "stay" keeps the key, so the selection
        # and the pane highlight do not jump to the first row. A move starts again from the top.
        return selected, collapsed
    if not visible:
        return "", collapsed
    if selected not in visible:
        return visible[0], collapsed
    agent, index = snapshot.by_key[selected], visible.index(selected)
    if action == "down":
        selected = visible[min(index + 1, len(visible) - 1)]
    elif action == "up":
        selected = visible[max(index - 1, 0)]
    elif action == "home":
        selected = visible[0]
    elif action == "end":
        selected = visible[-1]
    elif action == "toggle" and agent.children:
        collapsed ^= {selected}
    elif action == "right" and agent.children:
        if selected in collapsed:
            collapsed.discard(selected)
        else:
            selected = agent.children[0].key
    elif action == "left":
        if agent.children and selected not in collapsed:
            collapsed.add(selected)
        elif agent.parent_key in snapshot.by_key:
            selected = agent.parent_key
    return selected, collapsed


def too_small(screen_rows, screen_columns):
    """Takes the real screen size, before any column is held back."""
    return screen_rows < MIN_ROWS or screen_columns < MIN_COLUMNS
