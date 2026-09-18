"""The sidebar's highlight: the selected agent's pane gets a background tint and a coloured border.

Keyboard focus never moves, and everything is undone exactly.
- The tint is a pane option. The pane's own earlier values are saved in its option MARK.
- The border is not: in tmux 3.4 both border styles are window options. So while a pane is marked,
  its window's border styles become `#{?#{@agent_menu_highlight},<highlight>,<previous style>}`.
  tmux draws each pane's border with that pane's options, so only the marked pane's border changes
  and every other pane looks as before. The window's own earlier values are saved in WINDOW_MARK and
  put back once none of its panes is marked.
Saving happens before any change, so a later sidebar can undo what a killed one left. A pane or
window without a marker is never touched, and an option is put back only while it still holds what
this module wrote: a value the owner sets during a highlight is theirs and stays."""
import base64
import json
import os
import re

import menu_style
from sources import tmux_src
from sources.base import SourceError

MARK = "@agent_menu_highlight"
WINDOW_MARK = "@agent_menu_highlight_window"
COND_PREFIX = f"#{{?#{{{MARK}}},"                              # how a border style of ours starts
TINTS = ("window-style", "window-active-style")               # pane options
BORDERS = ("pane-border-style", "pane-active-border-style")   # window options
DEFAULT_BG, DEFAULT_BORDER = "colour236", "brightcyan"
# A colour name, colourNNN, or #rrggbb: nothing that could add style attributes or a tmux format.
COLOUR = re.compile(r"^(?:[A-Za-z]+[0-9]{0,3}|#[0-9A-Fa-f]{6})$")


def enabled(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("AGENT_MENU_HIGHLIGHT", "1") != "0" and menu_style.colour_allowed(environ)


def _colour(name, default):
    value = os.environ.get(name, "").strip()
    return value if COLOUR.match(value) else default


def styles():
    bg = _colour("AGENT_MENU_HIGHLIGHT_BG", DEFAULT_BG)
    border = _colour("AGENT_MENU_HIGHLIGHT_BORDER", DEFAULT_BORDER)
    return {"window-style": f"bg={bg}", "window-active-style": f"bg={bg}",
            "pane-border-style": f"fg={border}", "pane-active-border-style": f"fg={border},bold"}


def branch(style):
    """`style` made safe as one branch of #{?…,…,…}, keeping its meaning.
    tmux expands a style as a format only when it holds `#{`. The conditional does, so a plain style
    put inside it would be expanded too: `#DD4814` would lose `#D` to the pane id. So a plain style
    gets every `#`, `,` and `}` escaped. A style that is already a format keeps its own escapes, and
    only a comma outside its nested #{…}, which would end the branch, becomes `#,`."""
    if "#{" not in style:
        return style.replace("#", "##").replace(",", "#,").replace("}", "#}")
    out, depth, i = [], 0, 0
    while i < len(style):
        if style[i] == "#" and i + 1 < len(style):
            depth += style[i + 1] == "{"
            out.append(style[i:i + 2])
            i += 2
            continue
        if style[i] == "}" and depth:
            depth -= 1
        out.append("#," if style[i] == "," and not depth else style[i])
        i += 1
    return "".join(out)


def conditional(highlight, previous):
    return f"#{{?#{{{MARK}}},{branch(highlight)},{branch(previous or 'default')}}}"


def _encode(saved):
    # base64 keeps the value clear of anything tmux's own parser treats specially, such as `;` or `{`.
    return "v1:" + base64.urlsafe_b64encode(json.dumps(saved).encode()).decode()


def _decode(value):
    try:
        saved = json.loads(base64.urlsafe_b64decode(value[3:].encode())) if value.startswith("v1:") else None
    except ValueError:
        saved = None
    return saved if isinstance(saved, dict) else {}  # unreadable: the earlier values are lost


def apply(pane_id):
    """Save, then highlight the pane. Returns its window. Raises SourceError when tmux refuses.
    Each group of changes is one tmux call, marker first: tmux runs it whole even if this process dies
    meanwhile, and stops at a refused change with the marker already set to undo by."""
    if tmux_src.option(pane_id, MARK):
        restore(pane_id)  # a killed sidebar's highlight: undo it first, or it would be saved as the original
    look = styles()
    record = {"saved": {name: tmux_src.option(pane_id, name) or None for name in TINTS},
              "wrote": {name: look[name] for name in TINTS}}
    tmux_src.set_options([(pane_id, MARK, _encode(record), "p")] +
                         [(pane_id, name, look[name], "p") for name in TINTS])
    window = tmux_src.window_of(pane_id)
    if not tmux_src.option(window, WINDOW_MARK, "w"):
        record = {"saved": {name: tmux_src.option(window, name, "w") or None for name in BORDERS}}
        before = {name: tmux_src.option(window, name, "w", inherited=True) for name in BORDERS}
        tmux_src.set_options([(window, WINDOW_MARK, _encode(record), "w")] +
                             [(window, name, conditional(look[name], before[name]), "w") for name in BORDERS])
    return window


def release(window):
    """Give a window its own border styles back, once none of its panes is highlighted."""
    try:
        mark = tmux_src.option(window, WINDOW_MARK, "w")
        if not mark or tmux_src.panes_with_option(MARK, window):
            return
        saved = _decode(mark).get("saved") or {}
        tmux_src.set_options([(window, name, saved.get(name), "w") for name in BORDERS
                              if tmux_src.option(window, name, "w").startswith(COND_PREFIX)] +
                             [(window, WINDOW_MARK, None, "w")])
    except SourceError:
        pass


def restore(pane_id, window=None):
    """Put back what the pane, and then its window, had before. A pane that is gone is no error."""
    try:
        mark = tmux_src.option(pane_id, MARK)
        if mark:
            record = _decode(mark)
            saved = record.get("saved") or {}
            wrote = record.get("wrote") or {name: styles()[name] for name in TINTS}  # unreadable: judge by today's look
            tmux_src.set_options([(pane_id, name, saved.get(name), "p") for name in TINTS
                                  if tmux_src.option(pane_id, name) == wrote.get(name)] +
                                 [(pane_id, MARK, None, "p")])
        window = window or tmux_src.window_of(pane_id)
    except SourceError:
        pass
    if window:
        release(window)


def clear_stale():
    """Undo every highlight left behind: marked panes first, then windows none of whose panes is marked."""
    try:
        panes = tmux_src.panes_with_option(MARK)
        windows = tmux_src.windows_with_option(WINDOW_MARK)
    except SourceError:
        return
    for pane_id in panes:
        restore(pane_id)
    for window in windows:
        release(window)


class Highlighter:
    """Keeps the highlight on the selected agent's pane. Calls tmux only when that pane changes."""

    def __init__(self):
        self.pane, self.window, self.failed, self.on = None, None, None, enabled()

    def show(self, pane_id):
        pane_id = pane_id if self.on else None
        if pane_id and pane_id == self.failed:
            return  # tmux refused it: tried again only once the selection has moved
        self.failed = None
        if pane_id == self.pane:
            return
        old, old_window = self.pane, self.window
        self.pane = self.window = None
        if pane_id:
            # Recorded before any change, so an exit halfway through apply() still undoes what was done.
            self.pane = pane_id
            try:
                self.window = apply(pane_id)
            except SourceError:
                restore(pane_id)  # half applied, or the pane is gone: leave nothing behind
                self.pane, self.failed = None, pane_id
        if old:
            # After the new pane is marked: a move within one window keeps its border styles in place.
            restore(old, old_window)

    def clear(self):
        """Undo everything, including a move that an exit cut short."""
        self.pane = self.window = None
        clear_stale()
