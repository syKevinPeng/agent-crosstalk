"""The popup's key handling as a pure state machine, so it can be tested without a terminal.

Pasted text must never act. Ordinary words hold the hotkeys (`error` has `rr`), so two rules apply
outside the instruction line. A character that arrives within BURST seconds of the previous one is
part of a paste and is ignored.

The risky action needs a second, different kind of step: `r` arms it, and then only `y` or Enter
confirms, only once SETTLE seconds have passed, and only as the very next key. Anything else
disarms it, a pasted key included. That is what stops a paste arriving in chunks: a pause between
chunks looks like a person, but the characters in between disarm the button long before the chunk
that happens to contain a `y`.

Inside the instruction line a paste is welcome as text, but it must never send itself: a pasted
block that starts with `i` and ends with a newline would otherwise become an instruction to an
agent. So an Enter that arrives in a burst never sends. And once any of the text was pasted, the
Enter that sends it must come SETTLE seconds after the last key, which a person reviewing pasted
text does anyway. Text typed by hand sends on an ordinary Enter.

`step(state, offered, event)` returns the new state and, when the owner has really asked for one,
the action to perform. Events: ("char", "x"), ("key", "enter" | "esc" | "tab" | "backtab" | "left" |
"right" | "backspace" | "pgup" | "pgdn"), ("click", "<button>" | "input")."""
import dataclasses

HOTKEYS = {"o": "Open pane", "t": "Attach", "r": "Retire"}
RISKY = ("Retire",)          # armed first, then confirmed. Nothing else is.
BURST = 0.05                 # seconds. No one types two keys this fast; a paste always does
SETTLE = 0.6                 # seconds between arming and a confirm that counts


@dataclasses.dataclass(frozen=True)
class State:
    typing: bool = False
    text: str = ""
    confirm: str = ""        # the risky button waiting for its second press
    focus: int = 0
    scroll: int = 0
    close: bool = False
    armed_at: float = 0.0    # when `confirm` was set
    last_at: float = -1.0    # when the previous event arrived
    pasted: bool = False     # some of `text` arrived as a paste


def _press(state, button, now, confirming=False):
    """A button was chosen. A risky one is armed first. It acts only on a deliberate confirm
    (`confirming`) that comes after the settle time."""
    if button == "Send":
        if not state.text.strip():
            return dataclasses.replace(state, typing=True, confirm=""), ""
        return dataclasses.replace(state, typing=False, confirm=""), "Send"
    if button in RISKY:
        if confirming and state.confirm == button and now - state.armed_at >= SETTLE:
            return dataclasses.replace(state, confirm=""), button
        if state.confirm == button:
            return dataclasses.replace(state, confirm=""), ""   # too soon, or pressed again: disarm
        return dataclasses.replace(state, confirm=button, armed_at=now), ""
    return dataclasses.replace(state, confirm=""), button


def step(state, offered, event, page=10, now=0.0):
    """`now` is a monotonic clock in seconds. Tests pass their own."""
    since = now - state.last_at if state.last_at >= 0 else float("inf")
    state = dataclasses.replace(state, last_at=now)
    state, action = _step(state, offered, event, page, now, since < BURST, since)
    if not state.text:
        state = dataclasses.replace(state, pasted=False)       # an emptied line starts clean
    return state, action


def _step(state, offered, event, page, now, burst, since):
    kind, value = event
    if kind == "click":
        if value == "input" and "Send" in offered:
            return dataclasses.replace(state, typing=True, confirm=""), ""
        if value in offered:
            return _press(state, value, now, confirming=True)   # a second click on the armed button confirms
        return dataclasses.replace(state, confirm=""), ""

    if state.typing:                                   # while typing, letters are text, never hotkeys
        if (kind, value) == ("key", "esc"):
            return dataclasses.replace(state, typing=False), ""
        if (kind, value) == ("key", "enter"):
            if burst or (state.pasted and since < SETTLE):
                return state, ""                                # a paste cannot send itself
            return _press(state, "Send", now)
        if (kind, value) == ("key", "backspace"):
            return dataclasses.replace(state, text=state.text[:-1]), ""
        if kind == "char" and value.isprintable():
            return dataclasses.replace(state, text=state.text + value, pasted=state.pasted or burst), ""
        return state, ""

    if kind == "key":
        if value == "esc":
            return (dataclasses.replace(state, confirm=""), "") if state.confirm else \
                (dataclasses.replace(state, close=True), "")
        if value == "enter" and offered:
            if burst:
                return dataclasses.replace(state, confirm=""), ""   # a newline inside a paste disarms
            if state.confirm:
                return _press(state, state.confirm, now, confirming=True)
            return _press(state, offered[min(state.focus, len(offered) - 1)], now)
        if value in ("tab", "right") and offered:
            return dataclasses.replace(state, focus=(state.focus + 1) % len(offered), confirm=""), ""
        if value in ("backtab", "left") and offered:
            return dataclasses.replace(state, focus=(state.focus - 1) % len(offered), confirm=""), ""
        if value == "pgdn":
            return dataclasses.replace(state, scroll=state.scroll + page, confirm=""), ""
        if value == "pgup":
            return dataclasses.replace(state, scroll=max(state.scroll - page, 0), confirm=""), ""
        return dataclasses.replace(state, confirm=""), ""

    if burst:
        # Part of a paste. It presses nothing, and it disarms: a later chunk must not find the
        # button still armed and confirm it.
        return dataclasses.replace(state, confirm=""), ""
    if state.confirm:
        if value == "y":
            return _press(state, state.confirm, now, confirming=True)
        return dataclasses.replace(state, confirm=""), ""       # any other key cancels
    if value == "q":
        return dataclasses.replace(state, close=True), ""
    if value == "i" and "Send" in offered:
        return dataclasses.replace(state, typing=True, confirm=""), ""
    button = HOTKEYS.get(value, "")
    if button in offered:
        return _press(state, button, now)
    return state, ""


def cancel(state):
    """Disarm, without pressing anything."""
    return dataclasses.replace(state, confirm="")


def after_action(state, button, succeeded):
    """Clear the instruction once it was delivered. Keep it if it was refused, so nothing is lost."""
    sent = button == "Send" and succeeded
    return dataclasses.replace(state, text="" if sent else state.text, pasted=False if sent else state.pasted,
                               confirm="")
