"""The little curses shared by the sidebar and the popup: colours that respect the user's theme,
attributes by name, and writes that cannot crash on the last cell."""
import curses

import menu_style

PAIRS = {"red": 1, "yellow": 2, "cyan": 3}
ATTRS = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE}


def usable_terminal(environ=None):
    """A full-screen program needs a terminal that can move the cursor. TERM=dumb cannot."""
    import os
    environ = os.environ if environ is None else environ
    return environ.get("TERM", "") not in ("", "dumb", "unknown")


def setup(screen):
    """Default background everywhere (-1), the terminal's own 16 colours only, a short Esc delay."""
    colour = False
    try:
        curses.use_default_colors()
        if menu_style.colour_allowed(terminal_has_colours=curses.has_colors()):
            for name, pair in PAIRS.items():
                curses.init_pair(pair, getattr(curses, f"COLOR_{name.upper()}"), -1)
            colour = True
    except curses.error:
        pass
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
    screen.keypad(True)
    return colour


def look(state, colour):
    """curses attribute for a status. Without colour the attribute and the glyph still tell states apart."""
    name, attribute = menu_style.LOOK.get(state, ("", ""))
    value = ATTRS.get(attribute, 0)
    if state == "auth" and not colour:
        value |= curses.A_REVERSE
    if colour and name:
        value |= curses.color_pair(PAIRS[name])
    return value


def cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass  # a terminal such as TERM=dumb cannot hide the cursor. That is no reason to crash


def put(screen, y, x, text, attr=0):
    try:
        screen.addstr(y, x, text, attr)
    except curses.error:
        pass  # a write that touches the lower-right cell raises after it has printed
