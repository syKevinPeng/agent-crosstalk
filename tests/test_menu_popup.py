"""Tests for the popup's key handling, which is a pure state machine."""
import sys
import unittest

from menu_fixtures import LIB

sys.path.insert(0, str(LIB))
from menu_popup import State, after_action, step  # noqa: E402

ALL = ["Open pane", "Send", "Retire"]


def play(state, offered, events):
    actions = []
    for event in events:
        state, action = step(state, offered, event)
        if action:
            actions.append(action)
    return state, actions


def chars(text):
    return [("key", "enter") if c == "\n" else ("char", c) for c in text]


class PopupTest(unittest.TestCase):
    def test_retire_needs_the_same_choice_twice_and_any_other_key_cancels(self):
        self.assertEqual(play(State(), ALL, chars("r"))[1], [])
        self.assertEqual(play(State(), ALL, chars("rr"))[1], ["Retire"])
        self.assertEqual(play(State(), ALL, chars("rxr"))[1], [])                     # x cancelled the first r
        self.assertEqual(play(State(), ALL, [("char", "r"), ("key", "enter")])[1], ["Retire"])
        self.assertEqual(play(State(), ALL, [("click", "Retire"), ("click", "Retire")])[1], ["Retire"])
        self.assertEqual(play(State(), ALL, [("click", "Retire"), ("click", "Open pane")])[1], ["Open pane"])
        state, actions = play(State(), ALL, [("char", "r"), ("key", "esc")])
        self.assertEqual((actions, state.close, state.confirm), ([], False, ""))       # Esc cancels, does not close

    def test_other_buttons_act_at_once(self):
        self.assertEqual(play(State(), ALL, chars("o"))[1], ["Open pane"])
        self.assertEqual(play(State(), ["Attach"], chars("t"))[1], ["Attach"])
        self.assertEqual(play(State(), ALL, chars("t"))[1], [])                        # not offered, so not possible

    def test_while_typing_letters_are_text_never_hotkeys(self):
        state, actions = play(State(), ALL, chars("i") + chars("rr open retire q"))
        self.assertEqual(actions, [])
        self.assertEqual(state.text, "rr open retire q")
        self.assertFalse(state.close)

    def test_a_pasted_second_line_cannot_retire_or_open(self):
        state, actions = play(State(), ALL, chars("i") + chars("first line\n"))
        self.assertEqual(actions, ["Send"])
        self.assertEqual(state.text, "first line")
        # The loop flushes pending input after an action. Even if it did not, one `r` is not enough.
        state = after_action(state, "Send", succeeded=True)
        leftovers = play(state, ALL, chars("r second, then retire it r"))[1]
        self.assertNotIn("Retire", leftovers)      # harmless keys may fire, the risky one cannot

    def test_send_needs_text_and_keeps_it_when_refused(self):
        state, actions = play(State(), ALL, [("key", "tab"), ("key", "enter")])       # focus Send, press it, no text
        self.assertEqual((actions, state.typing), ([], True))
        state, actions = play(State(), ALL, chars("i") + chars("hello\n"))
        self.assertEqual(after_action(state, "Send", succeeded=False).text, "hello")
        self.assertEqual(after_action(state, "Send", succeeded=True).text, "")

    def test_typing_editing_and_leaving(self):
        state, _ = play(State(), ALL, chars("i") + chars("abc") + [("key", "backspace"), ("key", "backspace")])
        self.assertEqual(state.text, "a")
        state, _ = play(state, ALL, [("key", "backspace"), ("key", "backspace"), ("key", "esc")])
        self.assertEqual((state.text, state.typing, state.close), ("", False, False))
        self.assertTrue(play(state, ALL, [("key", "esc")])[0].close)
        self.assertTrue(play(State(), ALL, chars("q"))[0].close)
        self.assertEqual(play(State(), ["Open pane"], chars("i"))[0].typing, False)   # no Send, no typing

    def test_focus_wraps_and_scroll_never_goes_negative(self):
        state, _ = play(State(), ALL, [("key", "tab")] * 4)
        self.assertEqual(state.focus, 1)
        state, _ = play(State(), ALL, [("key", "backtab")])
        self.assertEqual(state.focus, 2)
        self.assertEqual(play(State(), ALL, [("key", "pgup")])[0].scroll, 0)
        self.assertEqual(play(State(), ALL, [("key", "pgdn")])[0].scroll, 10)
        self.assertEqual(play(State(), [], [("key", "enter"), ("key", "tab"), ("click", "Send")])[1], [])


if __name__ == "__main__":
    unittest.main()
