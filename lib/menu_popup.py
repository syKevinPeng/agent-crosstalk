"""The popup's key handling as a pure state machine, so it can be tested without a terminal.

`step(state, offered, event)` returns the new state and, when the owner has really asked for one,
the action to perform. Events: ("char", "x"), ("key", "enter" | "esc" | "tab" | "backtab" | "left" |
"right" | "backspace" | "pgup" | "pgdn"), ("click", "<button>" | "input")."""
import dataclasses

HOTKEYS = {"o": "Open pane", "t": "Attach", "r": "Retire"}
RISKY = ("Retire",)          # asked twice. Nothing else is.


@dataclasses.dataclass(frozen=True)
class State:
    typing: bool = False
    text: str = ""
    confirm: str = ""        # the risky button waiting for its second press
    focus: int = 0
    scroll: int = 0
    close: bool = False


def _press(state, button):
    """A button was chosen by key, Enter or click. Risky ones need the same choice twice in a row."""
    if button == "Send":
        if not state.text.strip():
            return dataclasses.replace(state, typing=True, confirm=""), ""
        return dataclasses.replace(state, typing=False, confirm=""), "Send"
    if button in RISKY and state.confirm != button:
        return dataclasses.replace(state, confirm=button), ""
    return dataclasses.replace(state, confirm=""), button


def step(state, offered, event, page=10):
    kind, value = event
    if kind == "click":
        if value == "input" and "Send" in offered:
            return dataclasses.replace(state, typing=True, confirm=""), ""
        if value in offered:
            return _press(state, value)
        return dataclasses.replace(state, confirm=""), ""

    if state.typing:                                   # while typing, letters are text, never hotkeys
        if (kind, value) == ("key", "esc"):
            return dataclasses.replace(state, typing=False), ""
        if (kind, value) == ("key", "enter"):
            return _press(state, "Send")
        if (kind, value) == ("key", "backspace"):
            return dataclasses.replace(state, text=state.text[:-1]), ""
        if kind == "char" and value.isprintable():
            return dataclasses.replace(state, text=state.text + value), ""
        return state, ""

    if kind == "key":
        if value == "esc":
            return (dataclasses.replace(state, confirm=""), "") if state.confirm else \
                (dataclasses.replace(state, close=True), "")
        if value == "enter" and offered:
            return _press(state, state.confirm or offered[min(state.focus, len(offered) - 1)])
        if value in ("tab", "right") and offered:
            return dataclasses.replace(state, focus=(state.focus + 1) % len(offered), confirm=""), ""
        if value in ("backtab", "left") and offered:
            return dataclasses.replace(state, focus=(state.focus - 1) % len(offered), confirm=""), ""
        if value == "pgdn":
            return dataclasses.replace(state, scroll=state.scroll + page, confirm=""), ""
        if value == "pgup":
            return dataclasses.replace(state, scroll=max(state.scroll - page, 0), confirm=""), ""
        return dataclasses.replace(state, confirm=""), ""

    if value == "q":
        return dataclasses.replace(state, close=True), ""
    if value == "i" and "Send" in offered:
        return dataclasses.replace(state, typing=True, confirm=""), ""
    button = HOTKEYS.get(value, "")
    if button in offered:
        return _press(state, button)
    return dataclasses.replace(state, confirm=""), ""           # any other key cancels a pending confirm


def after_action(state, button, succeeded):
    """Clear the instruction once it was delivered. Keep it if it was refused, so nothing is lost."""
    return dataclasses.replace(state, text="" if button == "Send" and succeeded else state.text, confirm="")
