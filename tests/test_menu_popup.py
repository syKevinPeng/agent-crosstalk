"""Tests for the popup's key handling, which is a pure state machine with a clock passed in."""
import curses
import sys
import unittest
from unittest import mock

from menu_fixtures import LIB

sys.path.insert(0, str(LIB))
import menu_curses  # noqa: E402
from menu_popup import SETTLE, State, after_action, cancel, step  # noqa: E402

ALL = ["Open pane", "Send", "Retire"]
HUMAN, PASTE = 1.0, 0.001          # seconds between events: a person, and a paste


def play(offered, events, gap=HUMAN, state=None, start=100.0):
    """Feed events `gap` seconds apart. An event may be (kind, value) or (kind, value, own_gap)."""
    state, now, actions = state or State(), start, []
    for event in events:
        now += event[2] if len(event) == 3 else gap
        state, action = step(state, offered, event[:2], now=now)
        if action:
            actions.append(action)
    return state, actions


def chars(text):
    return [("key", "enter") if c == "\n" else ("char", c) for c in text]


class PopupTest(unittest.TestCase):
    def test_retire_is_armed_by_r_and_confirmed_only_by_y_or_enter(self):
        self.assertEqual(play(ALL, chars("r"))[1], [])
        self.assertEqual(play(ALL, chars("ry"))[1], ["Retire"])
        self.assertEqual(play(ALL, chars("r") + [("key", "enter")])[1], ["Retire"])
        self.assertEqual(play(ALL, chars("rr"))[1], [])                    # pressing it again cancels
        self.assertEqual(play(ALL, chars("rry"))[1], [])                   # and stays cancelled
        self.assertEqual(play(ALL, chars("rxy"))[1], [])                   # x cancelled, so y confirms nothing
        self.assertEqual(play(ALL, chars("rrry"))[1], ["Retire"])          # armed, cancelled, armed, confirmed
        self.assertEqual(play(ALL, [("click", "Retire"), ("click", "Retire")])[1], ["Retire"])
        self.assertEqual(play(ALL, [("click", "Retire"), ("click", "Open pane")])[1], ["Open pane"])
        state, actions = play(ALL, chars("r") + [("key", "esc")])
        self.assertEqual((actions, state.close, state.confirm), ([], False, ""))   # Esc cancels, does not close

    def test_a_confirm_that_comes_too_soon_cancels_the_whole_thing(self):
        quick = SETTLE / 2
        for events in ([("char", "r"), ("char", "y", quick)],
                       [("char", "r"), ("key", "enter", quick)],
                       [("click", "Retire"), ("click", "Retire", quick)],
                       [("char", "r"), ("char", "y", quick), ("char", "y", SETTLE)]):
            with self.subTest(events=events):
                state, actions = play(ALL, events)
                self.assertEqual((actions, state.confirm), ([], ""))        # disarmed: r must be pressed again
        self.assertEqual(play(ALL, [("char", "r"), ("char", "y", quick),
                                    ("char", "r", 1.0), ("char", "y", 1.0)])[1], ["Retire"])

    def test_a_paste_arriving_in_chunks_never_retires(self):
        """Terminals deliver a big paste in chunks with a human-looking pause between them."""
        def chunked(pieces, between):
            state, now, actions = State(), 100.0, []
            for text, gap in ((piece, between) for piece in pieces):
                for index, letter in enumerate(text):
                    now += gap if index == 0 else PASTE
                    event = ("key", "enter") if letter == "\n" else ("char", letter)
                    state, action = step(state, ALL, event, now=now)
                    if action:
                        actions.append(action)
            return actions

        for pieces in (["rm a\n", "rm b\n", "rm c\n", "yes\n"],
                       ["run one", "run two", "run three", "yes"],
                       ["rest of it", "yes do"],
                       ["sorry, retry", "yes"],
                       ["retry that", "yes do it"],
                       ["r\n", "yes"],                      # a pasted newline must disarm too
                       ["fix the error\n", "y"]):
            for between in (0.1, 0.2, 0.3, 0.7):
                with self.subTest(pieces=pieces, between=between):
                    self.assertNotIn("Retire", chunked(pieces, between))
        self.assertEqual(chunked(["r"], 1.0) + chunked(["y"], 1.0), [])     # each alone does nothing

    def test_the_one_shape_this_cannot_judge_is_left_to_the_popup(self):
        """A chunk that is exactly `r`, a pause, then a chunk starting `y` is the same input a person
        makes when confirming. The state machine allows it; bin/agent-menu-detail then looks for keys
        still arriving behind the confirm and cancels when it finds any."""
        state, now, actions = State(), 100.0, []
        for letter, gap in [("r", 1.0)] + [(c, 0.7 if i == 0 else PASTE) for i, c in enumerate("yes please")]:
            now += gap
            state, action = step(state, ALL, ("char", letter), now=now)
            if action:
                actions.append(action)
        self.assertEqual(actions, ["Retire"])

    def test_cancel_disarms_without_pressing_anything(self):
        state, _ = play(ALL, chars("r"))
        self.assertEqual(state.confirm, "Retire")
        self.assertEqual(after_action(cancel(state), "Retire", succeeded=False).confirm, "")

    def test_pasted_text_outside_the_instruction_line_never_retires(self):
        for text in ("see the error above", "current", "clear\nnext", "rry", "ry\n", "r\ny", "arry carry",
                     "retire it\n\n", "rrrrrryyyy\n"):
            with self.subTest(text=text):
                self.assertNotIn("Retire", play(ALL, chars(text), gap=PASTE)[1])
        # Tab Tab Enter Enter as one burst must not do it either.
        burst = [("key", "tab"), ("key", "tab"), ("key", "enter"), ("key", "enter")]
        self.assertNotIn("Retire", play(ALL, burst, gap=PASTE)[1])
        # Nor a paste that begins slowly: r typed by hand, then a paste that happens to hold y.
        self.assertNotIn("Retire", play(ALL, [("char", "r")] + [(k, v, PASTE) for k, v in chars("yes please\n")])[1])

    def test_a_paste_fires_at_most_its_first_key(self):
        state, actions = play(ALL, chars("see the error above, open it"), gap=PASTE)
        self.assertEqual(actions, [])                                      # `s` is no hotkey, the rest is a burst
        self.assertEqual(play(ALL, chars("open"), gap=PASTE)[1], ["Open pane"])   # the one key a person could also press
        self.assertFalse(state.close)

    def test_other_buttons_act_at_once(self):
        self.assertEqual(play(ALL, chars("o"))[1], ["Open pane"])
        self.assertEqual(play(["Attach"], chars("t"))[1], ["Attach"])
        self.assertEqual(play(ALL, chars("t"))[1], [])                     # not offered, so not possible

    def test_while_typing_letters_are_text_never_hotkeys_even_in_a_paste(self):
        for gap in (HUMAN, PASTE):
            state, actions = play(ALL, chars("i") + chars("rry open retire q"), gap=gap)
            self.assertEqual(actions, [])
            self.assertEqual(state.text, "rry open retire q")
            self.assertFalse(state.close)

    def test_a_paste_can_fill_the_instruction_line_but_never_send_it(self):
        # One burst that begins with the typing hotkey and ends with a newline.
        state, actions = play(ALL, chars("i delete everything\n"), gap=PASTE)
        self.assertEqual(actions, [])
        self.assertTrue(state.pasted)
        # The owner opens the line by hand, then pastes two lines.
        state, actions = play(ALL, chars("i") + [(k, v, PASTE) for k, v in chars("first line\nry second\n")])
        self.assertEqual(actions, [])
        self.assertEqual(state.text, "first linery second")            # visible, reviewable, not sent
        # A paste that arrives in chunks: the newline opens a new chunk a moment later. Still not sent.
        chunked = chars("i") + [(k, v, PASTE) for k, v in chars("drop the table")] + [("key", "enter", SETTLE / 2)]
        self.assertEqual(play(ALL, chunked)[1], [])
        # A deliberate Enter after a pause sends what was pasted.
        state, actions = play(ALL, chunked + [("key", "enter", SETTLE * 2)])
        self.assertEqual(actions, ["Send"])
        self.assertFalse(after_action(state, "Send", succeeded=True).pasted)

    def test_hand_typed_text_sends_on_an_ordinary_enter(self):
        fast_typist = 0.12
        state, actions = play(ALL, chars("i") + chars("run the tests\n"), gap=fast_typist)
        self.assertEqual((actions, state.pasted), (["Send"], False))
        # Clearing a pasted line by hand makes it a fresh line again.
        events = chars("i") + [(k, v, PASTE) for k, v in chars("ab")] + [("key", "backspace"), ("key", "backspace")]
        state, _ = play(ALL, events)
        self.assertEqual((state.text, state.pasted), ("", False))

    def test_enter_after_the_instruction_line_was_withdrawn_sends_nothing(self):
        """A reload can take Send away while the line is open: the agent was retired, or now waits on a
        prompt. The Enter meant for the line then only closes it."""
        state, actions = play(ALL, chars("i") + chars("hello"))
        self.assertEqual((actions, state.typing), ([], True))
        state, actions = play(["Open pane", "Retire"], [("key", "enter")], state=state, start=200.0)
        self.assertEqual((actions, state.typing, state.text), ([], False, "hello"))

    def test_send_needs_text_and_keeps_it_when_refused(self):
        state, actions = play(ALL, [("key", "tab"), ("key", "enter")])     # focus Send, press it, no text
        self.assertEqual((actions, state.typing), ([], True))
        state, actions = play(ALL, chars("i") + chars("hello\n"))
        self.assertEqual(actions, ["Send"])
        self.assertEqual(after_action(state, "Send", succeeded=False).text, "hello")
        self.assertEqual(after_action(state, "Send", succeeded=True).text, "")

    def test_typing_editing_and_leaving(self):
        state, _ = play(ALL, chars("i") + chars("abc") + [("key", "backspace"), ("key", "backspace")])
        self.assertEqual(state.text, "a")
        state, _ = play(ALL, [("key", "backspace"), ("key", "backspace"), ("key", "esc")], state=state)
        self.assertEqual((state.text, state.typing, state.close), ("", False, False))
        self.assertTrue(play(ALL, [("key", "esc")], state=state)[0].close)
        self.assertTrue(play(ALL, chars("q"))[0].close)
        self.assertEqual(play(["Open pane"], chars("i"))[0].typing, False)  # no Send, no typing

    def test_only_a_press_or_a_click_counts_as_a_click(self):
        """After a press held longer than a click, ncurses reports the release on its own at the next
        input, such as a wheel turn. Taken for a second click, it confirmed an armed Retire."""
        for bstate, expected in ((curses.BUTTON1_CLICKED, (4, 21)), (curses.BUTTON1_PRESSED, (4, 21)),
                                 (curses.BUTTON1_RELEASED, None), (0, None)):
            with self.subTest(bstate=bstate), \
                    mock.patch.object(menu_curses.curses, "getmouse", return_value=(0, 4, 21, 0, bstate)):
                self.assertEqual(menu_curses.click(), expected)

    def test_focus_wraps_and_scroll_never_goes_negative(self):
        self.assertEqual(play(ALL, [("key", "tab")] * 4)[0].focus, 1)
        self.assertEqual(play(ALL, [("key", "backtab")])[0].focus, 2)
        self.assertEqual(play(ALL, [("key", "pgup")])[0].scroll, 0)
        self.assertEqual(play(ALL, [("key", "pgdn")])[0].scroll, 10)
        self.assertEqual(play([], [("key", "enter"), ("key", "tab"), ("click", "Send")])[1], [])


if __name__ == "__main__":
    unittest.main()
