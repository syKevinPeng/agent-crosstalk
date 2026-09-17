"""Turn a Snapshot into sidebar rows. Pure functions: no curses, no I/O."""
import unicodedata

import menu_style

MIN_COLUMNS, MIN_ROWS = 20, 5
HINT_FULL = " ⏎ open  ←→ fold  ? help  q quit"
HINT_SHORT = " ⏎ open  ? help"
HINT_ASCII = " Enter open  ? help  q quit"
# Every help line fits the narrowest sidebar (32 cells) and the list fits 12 rows, so nothing is cut.
HELP = ["KEYS", "↑ ↓  j k   move", "→         open, then first child", "←         fold, then to parent",
        "Space     fold or unfold", "Home End  first, last", "Enter     open detail popup",
        "?  help       q  quit", "Click a row to open it.", "Click its arrow to fold it.", "Press any key."]
HELP_ASCII = ["KEYS", "up down j k   move", "right     open, then first child", "left      fold, then to parent",
              "Space     fold or unfold", "Home End  first, last", "Enter     open detail popup",
              "?  help       q  quit", "Click a row to open it.", "Click its arrow to fold it.", "Press any key."]
MAX_INDENT = 4


def width(text):
    """Screen cells. A combining mark sits on the character before it and takes none."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


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


def status(agent):
    """The one state a row shows, most urgent first. A retired agent shows nothing but retired."""
    if agent.retired:
        return "stopped"
    if agent.auth:
        return "auth"
    if agent.needs_owner:
        return "needs"
    if agent.open_messages:
        return "unanswered"
    return agent.state if agent.state in ("working", "idle") else "stopped"


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
    agents = snapshot.by_key.values()
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


def rows(snapshot, columns, collapsed=(), glyphs=None, frame=0):
    """[{text, key, style, mark, look}] for the sidebar. `text` always fits `columns` and ends with
    `mark`, so the drawing layer can colour the mark alone. style: header | error | row | retired."""
    glyphs = glyphs or menu_style.UNICODE
    out = [{"text": fit(header(snapshot, columns, glyphs), columns, glyphs["ellipsis"]),
            "key": "", "style": "header", "mark": "", "look": ""}]
    for error in snapshot.errors:
        out.append({"text": fit(f" {error}", columns, glyphs["ellipsis"]), "key": "", "style": "error",
                    "mark": "", "look": ""})

    def add(agent, prefix, fold):
        sign = mark(agent, glyphs, frame)
        kind = "retired" if agent.retired else f"{agent.access + ' ' if agent.access else ''}{agent.kind}"
        right = f"{kind} {sign}"
        room = columns - width(prefix) - width(fold) - width(right) - 2
        name = fit(agent.name, max(room, 4), glyphs["ellipsis"])
        gap = " " * max(columns - width(prefix) - width(fold) - width(name) - width(right) - 1, 1)
        out.append({"text": f"{prefix}{fold}{name}{gap}{right}", "key": agent.key,
                    "style": "retired" if agent.retired else "row", "mark": sign, "look": status(agent)})

    drawn = set()

    def walk(agent, depth, last):
        if agent.key in drawn:
            return  # a loop in the records must not draw, or recurse, forever
        drawn.add(agent.key)
        folded = agent.key in collapsed
        if depth == 0:
            add(agent, " ", (glyphs["closed"] if folded else glyphs["open"]) if agent.children else glyphs["leaf"])
        else:
            add(agent, " " + "  " * min(depth, MAX_INDENT), glyphs["last"] if last else glyphs["branch"])
        if not folded:
            for index, child in enumerate(agent.children):
                walk(child, depth + 1, index == len(agent.children) - 1)

    for root in snapshot.roots:
        walk(root, 0, True)
    return out


def scroll_top(rendered, selected, room, top):
    """First row to draw so that the selected row is on screen. Rows 0.. hold the header and any
    error lines, which stay pinned, so `room` is what is left under them for agent rows."""
    pinned = sum(1 for row in rendered if not row["key"])
    body = [row["key"] for row in rendered[pinned:]]
    if selected not in body or room <= 0:
        return 0
    index = body.index(selected)
    top = min(top, index)
    top = max(top, index - room + 1)
    return max(0, min(top, max(len(body) - room, 0)))


def key_at(rendered, row):
    """The agent key on screen row `row`, or "" for the header, an error line or empty space."""
    return rendered[row]["key"] if 0 <= row < len(rendered) else ""


def move(snapshot, collapsed, selected, action):
    """Tree keys after the WAI-ARIA tree pattern. Returns (selected, collapsed)."""
    collapsed = set(collapsed)
    visible = [r["key"] for r in rows(snapshot, 200, collapsed) if r["key"]]
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
