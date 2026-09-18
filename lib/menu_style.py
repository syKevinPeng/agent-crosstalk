"""How the menu looks: glyphs, the ASCII fallback, and when colour is allowed. Pure, no curses.

Choices follow published guidance: only glyphs of narrow width and no emoji, because an emoji is two
cells wide and tmux and the terminal can disagree about it (UAX #11, tmux issue 647). Every state has
its own shape, so colour is never the only cue (WCAG 1.4.1). At most three states get a colour
(clig.dev). NO_COLOR and TERM=dumb switch colour off and FORCE_COLOR switches it back on
(no-color.org, force-color.org). Bold, dim and reverse stay on either way."""
import locale
import os

UNICODE = {"auth": "✗", "needs": "!", "unanswered": "✉", "idle": "✓", "stopped": "-", "running": "?",
           "spinner": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "open": "▾ ", "closed": "▸ ", "leaf": "  ",
           "branch": "├ ", "last": "└ ", "ellipsis": "…", "separator": " · ", "arrow_out": "→", "arrow_in": "←",
           "bar_full": "█", "bar_empty": "░", "rule": "─"}
ASCII = {"auth": "X", "needs": "!", "unanswered": "+", "idle": ".", "stopped": "-", "running": "?",
         "spinner": "*", "open": "v ", "closed": "> ", "leaf": "  ",
         "branch": "|-", "last": "`-", "ellipsis": "~", "separator": " | ", "arrow_out": ">", "arrow_in": "<",
         "bar_full": "#", "bar_empty": ".", "rule": "-"}

# state -> (colour name or "", attribute name or ""). Only three states carry a colour.
LOOK = {"auth": ("red", "bold"), "needs": ("yellow", "reverse"), "unanswered": ("cyan", ""),
        "working": ("", ""), "idle": ("", "dim"), "stopped": ("", "dim"), "running": ("", "")}


def use_ascii(environ=None, encoding=None):
    environ = os.environ if environ is None else environ
    if environ.get("AGENT_MENU_ASCII"):
        return True
    # Python's UTF-8 mode reports UTF-8 even under LANG=C, so the locale the user set is read first.
    chosen = next((environ[v] for v in ("LC_ALL", "LC_CTYPE", "LANG") if environ.get(v)), "")
    if chosen.split(".")[0] in ("C", "POSIX") and "utf" not in chosen.lower().replace("-", ""):
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
