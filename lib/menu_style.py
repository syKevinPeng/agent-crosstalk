"""How the menu looks: glyphs, the ASCII fallback, and when colour is allowed. Pure, no curses.

Choices follow published guidance: only glyphs of narrow width and no emoji, because an emoji is two
cells wide and tmux and the terminal can disagree about it (UAX #11, tmux issue 647). Every state has
its own shape, so colour is never the only cue (WCAG 1.4.1). At most three states get a colour
(clig.dev). NO_COLOR and TERM=dumb switch colour off and FORCE_COLOR switches it back on
(no-color.org, force-color.org). Bold, dim and reverse stay on either way."""
import locale
import os

UNICODE = {"auth": "✗", "needs": "!", "unanswered": "✉", "idle": "✓", "stopped": "-",
           "spinner": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "open": "▾ ", "closed": "▸ ", "leaf": "  ",
           "branch": "├ ", "last": "└ ", "deep": "· ", "ellipsis": "…", "separator": " · ",
           "bar_full": "█", "bar_empty": "░", "rule": "─"}
ASCII = {"auth": "X", "needs": "!", "unanswered": "+", "idle": ".", "stopped": "-",
         "spinner": "*", "open": "v ", "closed": "> ", "leaf": "  ",
         "branch": "|-", "last": "`-", "deep": "- ", "ellipsis": "~", "separator": " | ",
         "bar_full": "#", "bar_empty": ".", "rule": "-"}

# state -> (colour name or "", attribute name or ""). Only three states carry a colour.
LOOK = {"auth": ("red", "bold"), "needs": ("yellow", "reverse"), "unanswered": ("cyan", ""),
        "working": ("", ""), "idle": ("", "dim"), "stopped": ("", "dim")}


def use_ascii(environ=None, encoding=None):
    environ = os.environ if environ is None else environ
    if environ.get("AGENT_MENU_ASCII"):
        return True
    encoding = encoding if encoding is not None else locale.getpreferredencoding(False)
    return "utf" not in (encoding or "").lower()


def glyphs(environ=None, encoding=None):
    return ASCII if use_ascii(environ, encoding) else UNICODE


def colour_allowed(environ=None, terminal_has_colours=True):
    environ = os.environ if environ is None else environ
    if environ.get("FORCE_COLOR"):
        return True
    if environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return False
    return bool(terminal_has_colours)


def animate(environ=None):
    environ = os.environ if environ is None else environ
    return not environ.get("AGENT_MENU_NO_ANIMATION")
