"""Tests for the popup's key handling, which is a pure state machine with a clock passed in."""
import curses
import sys
import unittest
from unittest import mock

from menu_fixtures import LIB

sys.path.insert(0, str(LIB))
import menu_curses  # noqa: E402
from menu_popup import SETTLE, State, after_action, cancel, step  # noqa: E402

LIVE = ["Open pane", "Send", "Quit agent"]      # what a live agent's popup offers
QUIT = ["Resume"]                                # what a quit agent's popup offers
HUMAN, PASTE = 1.0, 0.001                        # seconds between events: a person, and a paste


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


def chunked(offered, pieces, between):
    """Terminals deliver a big paste in chunks with a human-looking pause between them."""
    state, now, actions = State(), 100.0, []
    for text in pieces:
        for index, letter in enumerate(text):
            now += between if index == 0 else PASTE
            event = ("key", "enter") if letter == "\n" else ("char", letter)
            state, action = step(state, offered, event, now=now)
            if action:
                actions.append(action)
    return actions


class QuitTest(unittest.TestCase):
    def test_quit_is_armed_by_x_and_confirmed_only_by_y_or_enter(self):
        self.assertEqual(play(LIVE, chars("x"))[1], [])
        self.assertEqual(play(LIVE, chars("xy"))[1], ["Quit agent"])
        self.assertEqual(play(LIVE, chars("x") + [("key", "enter")])[1], ["Quit agent"])
        self.assertEqual(play(LIVE, chars("xx"))[1], [])                    # pressing it again cancels
        self.assertEqual(play(LIVE, chars("xxy"))[1], [])                   # and stays cancelled
        self.assertEqual(play(LIVE, chars("xzy"))[1], [])                   # z cancelled, so y confirms nothing
        self.assertEqual(play(LIVE, chars("xxxy"))[1], ["Quit agent"])      # armed, cancelled, armed, confirmed
        self.assertEqual(play(LIVE, [("click", "Quit agent"), ("click", "Quit agent")])[1], ["Quit agent"])
        self.assertEqual(play(LIVE, [("click", "Quit agent"), ("click", "Open pane")])[1], ["Open pane"])
        state, actions = play(LIVE, chars("x") + [("key", "esc")])
        self.assertEqual((actions, state.close, state.confirm), ([], False, ""))   # Esc cancels, does not close

    def test_q_still_closes_and_never_quits_an_agent(self):
        state, actions = play(LIVE, chars("q"))
        self.assertEqual((actions, state.close), ([], True))

    def test_a_confirm_that_comes_too_soon_cancels_the_whole_thing(self):
        quick = SETTLE / 2
        for events in ([("char", "x"), ("char", "y", quick)],
                       [("char", "x"), ("key", "enter", quick)],
                       [("click", "Quit agent"), ("click", "Quit agent", quick)],
                       [("char", "x"), ("char", "y", quick), ("char", "y", SETTLE)]):
            with self.subTest(events=events):
                state, actions = play(LIVE, events)
                self.assertEqual((actions, state.confirm), ([], ""))       # disarmed: x must be pressed again
        self.assertEqual(play(LIVE, [("char", "x"), ("char", "y", quick),
                                     ("char", "x", 1.0), ("char", "y", 1.0)])[1], ["Quit agent"])

    def test_a_paste_never_quits_an_agent(self):
        for text in ("fix the next text", "exit\nyes", "xy", "x\ny", "extra yes\n", "xxxxyyyy\n"):
            with self.subTest(text=text):
                self.assertNotIn("Quit agent", play(LIVE, chars(text), gap=PASTE)[1])
        burst = [("key", "tab"), ("key", "tab"), ("key", "enter"), ("key", "enter")]
        self.assertNotIn("Quit agent", play(LIVE, burst, gap=PASTE)[1])

    def test_a_paste_arriving_in_chunks_never_quits_an_agent(self):
        for pieces in (["fix a\n", "fix b\n", "fix c\n", "yes\n"],
                       ["next one", "next two", "next three", "yes"],
                       ["exit it", "yes do"],
                       ["x\n", "yes"],                         # a pasted newline must disarm too
                       ["the text\n", "y"]):
            for between in (0.1, 0.2, 0.3, 0.7):
                with self.subTest(pieces=pieces, between=between):
                    self.assertNotIn("Quit agent", chunked(LIVE, pieces, between))
        self.assertEqual(chunked(LIVE, ["x"], 1.0) + chunked(LIVE, ["y"], 1.0), [])   # each alone does nothing

    def test_the_one_shape_this_cannot_judge_is_left_to_the_popup(self):
        """A chunk that is exactly `x`, a pause, then a chunk starting `y` is the same input a person
        makes when confirming. The state machine allows it; bin/agent-menu-detail then looks for keys
        still arriving behind the confirm and cancels when it finds any."""
        self.assertEqual(chunked(LIVE, ["x", "yes please"], 0.7), ["Quit agent"])

    def test_cancel_disarms_without_pressing_anything(self):
        state, _ = play(LIVE, chars("x"))
        self.assertEqual(state.confirm, "Quit agent")
        self.assertEqual(after_action(cancel(state), "Quit agent", succeeded=False).confirm, "")


class ResumeTest(unittest.TestCase):
    def test_r_resumes_a_quit_agent_with_one_press(self):
        self.assertEqual(play(QUIT, chars("r"))[1], ["Resume"])
        self.assertEqual(play(QUIT, [("click", "Resume")])[1], ["Resume"])
        self.assertEqual(play(QUIT, [("key", "enter")])[1], ["Resume"])     # the focused button

    def test_r_does_nothing_where_resume_is_not_offered(self):
        self.assertEqual(play(LIVE, chars("r"))[1], [])

    def test_a_pasted_r_does_not_resume(self):
        self.assertEqual(play(QUIT, chars("error report"), gap=PASTE)[1], [])


class TypingTest(unittest.TestCase):
    def test_other_buttons_act_at_once(self):
        self.assertEqual(play(LIVE, chars("o"))[1], ["Open pane"])
        self.assertEqual(play(["Attach"], chars("t"))[1], ["Attach"])
        self.assertEqual(play(LIVE, chars("t"))[1], [])                     # not offered, so not possible

    def test_while_typing_letters_are_text_never_hotkeys_even_in_a_paste(self):
        for gap in (HUMAN, PASTE):
            state, actions = play(LIVE, chars("i") + chars("xy open resume q"), gap=gap)
            self.assertEqual(actions, [])
            self.assertEqual(state.text, "xy open resume q")
            self.assertFalse(state.close)

    def test_a_paste_can_fill_the_instruction_line_but_never_send_it(self):
        state, actions = play(LIVE, chars("i delete everything\n"), gap=PASTE)
        self.assertEqual(actions, [])
        self.assertTrue(state.pasted)
        state, actions = play(LIVE, chars("i") + [(k, v, PASTE) for k, v in chars("first line\nxy second\n")])
        self.assertEqual(actions, [])
        self.assertEqual(state.text, "first linexy second")
        pasted = chars("i") + [(k, v, PASTE) for k, v in chars("drop the table")] + [("key", "enter", SETTLE / 2)]
        self.assertEqual(play(LIVE, pasted)[1], [])
        state, actions = play(LIVE, pasted + [("key", "enter", SETTLE * 2)])
        self.assertEqual(actions, ["Send"])
        self.assertFalse(after_action(state, "Send", succeeded=True).pasted)

    def test_hand_typed_text_sends_on_an_ordinary_enter(self):
        state, actions = play(LIVE, chars("i") + chars("run the tests\n"), gap=0.12)
        self.assertEqual((actions, state.pasted), (["Send"], False))
        events = chars("i") + [(k, v, PASTE) for k, v in chars("ab")] + [("key", "backspace"), ("key", "backspace")]
        state, _ = play(LIVE, events)
        self.assertEqual((state.text, state.pasted), ("", False))

    def test_enter_after_the_instruction_line_was_withdrawn_sends_nothing(self):
        """A reload can take Send away while the line is open: the agent was quit, or now waits on a
        prompt. The Enter meant for the line then only closes it."""
        state, actions = play(LIVE, chars("i") + chars("hello"))
        self.assertEqual((actions, state.typing), ([], True))
        state, actions = play(["Open pane", "Quit agent"], [("key", "enter")], state=state, start=200.0)
        self.assertEqual((actions, state.typing, state.text), ([], False, "hello"))

    def test_send_needs_text_and_keeps_it_when_refused(self):
        state, actions = play(LIVE, [("key", "tab"), ("key", "enter")])     # focus Send, press it, no text
        self.assertEqual((actions, state.typing), ([], True))
        state, actions = play(LIVE, chars("i") + chars("hello\n"))
        self.assertEqual(actions, ["Send"])
        self.assertEqual(after_action(state, "Send", succeeded=False).text, "hello")
        self.assertEqual(after_action(state, "Send", succeeded=True).text, "")

    def test_typing_editing_and_leaving(self):
        state, _ = play(LIVE, chars("i") + chars("abc") + [("key", "backspace"), ("key", "backspace")])
        self.assertEqual(state.text, "a")
        state, _ = play(LIVE, [("key", "backspace"), ("key", "backspace"), ("key", "esc")], state=state)
        self.assertEqual((state.text, state.typing, state.close), ("", False, False))
        self.assertTrue(play(LIVE, [("key", "esc")], state=state)[0].close)
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
        self.assertEqual(play(LIVE, [("key", "tab")] * 4)[0].focus, 1)
        self.assertEqual(play(LIVE, [("key", "backtab")])[0].focus, 2)
        self.assertEqual(play(LIVE, [("key", "pgup")])[0].scroll, 0)
        self.assertEqual(play(LIVE, [("key", "pgdn")])[0].scroll, 10)
        self.assertEqual(play([], [("key", "enter"), ("key", "tab"), ("click", "Send")])[1], [])


if __name__ == "__main__":
    unittest.main()
