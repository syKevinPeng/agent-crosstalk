"""Tests for the sidebar's highlight of the selected agent's pane, against the stub tmux, which keeps
pane options as files. They check each pane's end state, not only the calls, and that focus never moves."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from menu_fixtures import BIN, LIB, Machine

sys.path.insert(0, str(LIB))
import menu_highlight  # noqa: E402

CLAUDE_A = "aaaaaaaa-0000-4000-8000-000000000001"
TINT = {"window-style": "bg=colour236", "window-active-style": "bg=colour236"}
# tmux 3.4 keeps border styles per window, so the window's styles depend on the pane's marker.
BORDER = {"pane-border-style": "#{?#{@agent_menu_highlight},fg=brightcyan,default}",
          "pane-active-border-style": "#{?#{@agent_menu_highlight},fg=brightcyan#,bold,default}"}
FOCUS = ("select-pane", "select-window", "switch-client")


class HighlightTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hl")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m.pane(7, 700, "claude", "lead")
        self.m.pane(8, 800, "claude", "helper")

    def own(self, options, mark):
        """Options without the marker, whose value is an encoding detail."""
        return {k: v for k, v in options.items() if k != mark}

    def tint(self, number):
        return self.own(self.m.pane_options(number), menu_highlight.MARK)

    def borders(self, number=1):
        return self.own(self.m.window_options(number), menu_highlight.WINDOW_MARK)

    def untouched(self):
        for number in (7, 8):
            self.assertEqual(self.m.pane_options(number), {}, number)
        self.assertEqual(self.m.window_options(1), {})

    def test_the_selected_pane_is_tinted_its_window_border_follows_it_and_focus_stays(self):
        menu_highlight.Highlighter().show("%7")
        self.assertEqual(self.tint(7), TINT)
        self.assertIn(menu_highlight.MARK, self.m.pane_options(7))
        self.assertEqual(self.m.pane_options(8), {})
        self.assertEqual(self.borders(), BORDER)
        self.assertFalse([c for c in self.m.tmux_calls() if c.startswith(FOCUS)])

    def test_the_same_selection_again_makes_no_tmux_call(self):
        light = menu_highlight.Highlighter()
        light.show("%7")
        before = len(self.m.tmux_calls())
        light.show("%7")
        self.assertEqual(len(self.m.tmux_calls()), before)

    def test_moving_on_puts_everything_back_exactly(self):
        (self.m.dir / "popt-7-window-style").write_text("bg=#101010")       # the pane's own earlier value
        (self.m.dir / "wopt-1-pane-border-style").write_text("fg=blue,bold")  # the window's own earlier value
        light = menu_highlight.Highlighter()
        light.show("%7")
        self.assertEqual(self.borders()["pane-border-style"],
                         "#{?#{@agent_menu_highlight},fg=brightcyan,fg=blue#,bold}")
        calls = len(self.m.tmux_calls())
        light.show("%8")                                                  # same window: borders stay
        self.assertEqual(self.borders()["pane-border-style"],
                         "#{?#{@agent_menu_highlight},fg=brightcyan,fg=blue#,bold}")
        self.assertFalse([c for c in self.m.tmux_calls()[calls:] if c.startswith("set-option -w")])
        self.assertEqual(self.m.pane_options(7), {"window-style": "bg=#101010"})
        self.assertEqual(self.tint(8), TINT)
        light.show(None)                                                  # an agent with no pane
        self.assertEqual(self.m.pane_options(8), {})
        self.assertEqual(self.m.window_options(1), {"pane-border-style": "fg=blue,bold"})

    def test_a_move_to_another_window_releases_the_first(self):
        (self.m.dir / "window-8").write_text("@2")
        light = menu_highlight.Highlighter()
        light.show("%7")
        light.show("%8")
        self.assertEqual(self.m.window_options(1), {})
        self.assertEqual(self.borders(2), BORDER)
        light.clear()
        self.assertEqual(self.m.window_options(2), {})

    def test_the_previous_style_keeps_its_meaning_inside_the_condition(self):
        # tmux expands the whole conditional as a format, so a plain `#DD4814` would lose `#D`.
        (self.m.dir / "gopt-pane-active-border-style").write_text("bg=#DD4814,fg=#DD4814")
        menu_highlight.Highlighter().show("%7")
        self.assertEqual(self.borders()["pane-active-border-style"],
                         "#{?#{@agent_menu_highlight},fg=brightcyan#,bold,bg=##DD4814#,fg=##DD4814}")

    def test_branch_escapes_a_plain_style_and_leaves_a_format_its_own_commas(self):
        branch = menu_highlight.branch
        self.assertEqual(branch("fg=#75507B"), "fg=##75507B")
        self.assertEqual(branch("fg=blue,bold"), "fg=blue#,bold")
        fmt = "#{?pane_in_mode,fg=yellow,#{?synchronize-panes,fg=red,fg=green}}"   # tmux's own default
        self.assertEqual(branch(fmt), fmt)
        self.assertEqual(branch("#{?x,a,b},bold"), "#{?x,a,b}#,bold")

    def test_what_a_killed_sidebar_left_is_undone_at_start(self):
        (self.m.dir / "popt-7-window-style").write_text("bg=#101010")
        (self.m.dir / "wopt-1-pane-border-style").write_text("fg=blue")
        menu_highlight.Highlighter().show("%7")                           # and then the sidebar dies
        menu_highlight.clear_stale()
        self.assertEqual(self.m.pane_options(7), {"window-style": "bg=#101010"})
        self.assertEqual(self.m.window_options(1), {"pane-border-style": "fg=blue"})

    def test_a_window_left_marked_with_no_marked_pane_is_released_at_start(self):
        menu_highlight.Highlighter().show("%7")
        (self.m.dir / f"popt-7-{menu_highlight.MARK}").unlink()           # the pane went, its window stayed
        menu_highlight.clear_stale()
        self.assertEqual(self.m.window_options(1), {})

    def test_a_leftover_on_the_newly_selected_pane_never_becomes_its_original(self):
        (self.m.dir / "popt-7-window-style").write_text("bg=#101010")
        menu_highlight.Highlighter().show("%7")                           # a killed sidebar's highlight
        light = menu_highlight.Highlighter()
        light.show("%7")
        light.show(None)
        self.assertEqual(self.m.pane_options(7), {"window-style": "bg=#101010"})
        self.assertEqual(self.m.window_options(1), {})

    def test_a_pane_or_window_without_a_marker_is_never_touched(self):
        (self.m.dir / "popt-8-window-style").write_text("bg=red")
        (self.m.dir / "wopt-1-pane-border-style").write_text("fg=red")
        menu_highlight.clear_stale()
        menu_highlight.restore("%8")
        menu_highlight.release("@1")
        self.assertEqual(self.m.pane_options(8), {"window-style": "bg=red"})
        self.assertEqual(self.m.window_options(1), {"pane-border-style": "fg=red"})

    def test_off_switches_and_force_colour(self):
        for env, lit in (({"AGENT_MENU_HIGHLIGHT": "0"}, False), ({"NO_COLOR": "1"}, False),
                         ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, True)):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                light = menu_highlight.Highlighter()
                light.show("%7")
                self.assertEqual(bool(self.tint(7)), lit)
                self.assertEqual(bool(self.borders()), lit)
                light.clear()
        self.untouched()

    def test_colours_can_be_chosen_and_odd_values_are_ignored(self):
        with mock.patch.dict(os.environ, {"AGENT_MENU_HIGHLIGHT_BG": "colour24",
                                          "AGENT_MENU_HIGHLIGHT_BORDER": "#ff8800"}):
            light = menu_highlight.Highlighter()
            light.show("%7")
        self.assertEqual(self.tint(7)["window-style"], "bg=colour24")
        self.assertEqual(self.borders()["pane-border-style"], "#{?#{@agent_menu_highlight},fg=##ff8800,default}")
        light.clear()
        with mock.patch.dict(os.environ, {"AGENT_MENU_HIGHLIGHT_BG": "red,blink",
                                          "AGENT_MENU_HIGHLIGHT_BORDER": "#(touch x)"}):
            menu_highlight.Highlighter().show("%7")
        self.assertEqual(self.tint(7), TINT)
        self.assertEqual(self.borders(), BORDER)

    def test_an_exit_in_the_middle_of_a_highlight_still_undoes_it(self):
        light = menu_highlight.Highlighter()
        real = menu_highlight.tmux_src.set_option
        for stop_at in ("window-active-style", "pane-border-style", "pane-active-border-style"):
            with self.subTest(stop_at=stop_at):
                def interrupted(target, name, value=None, scope="p"):
                    if name == stop_at and value:
                        raise SystemExit(143)                    # SIGTERM arrives halfway through
                    return real(target, name, value, scope)
                with mock.patch.object(menu_highlight.tmux_src, "set_option", interrupted):
                    with self.assertRaises(SystemExit):
                        light.show("%7")
                light.clear()
                self.untouched()

    def test_an_exit_between_marking_the_new_pane_and_releasing_the_old_one_is_undone(self):
        light = menu_highlight.Highlighter()
        light.show("%7")
        with mock.patch.object(menu_highlight, "restore", side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                light.show("%8")
        light.clear()
        self.untouched()

    def test_a_pane_that_is_gone_is_no_error(self):
        (self.m.dir / "gone-7").write_text("")
        light = menu_highlight.Highlighter()
        light.show("%7")
        light.show("%8")
        self.assertEqual(self.tint(8), TINT)


class SidebarHighlightTest(unittest.TestCase):
    """The real sidebar on a pty: it highlights the selected agent's pane and undoes it when it ends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hs")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        self.m.claude_session(CLAUDE_A, "lead", 110)
        self.m.pane(1, 100, "claude", "lead")
        self.m.process(110, 100)

    def sidebar(self, ending):
        import pty
        import threading
        env = dict(self.m.env, TERM="xterm-256color", LINES="30", COLUMNS="40")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(BIN / "agent-menu")], env)

        def drain():                                   # a pty whose output is never read blocks the writer
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        try:
            deadline = time.monotonic() + 20
            while "window-style" not in self.m.pane_options(1) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.m.pane_options(1).get("window-style"), "bg=colour236")
            if ending == "q":
                os.write(fd, b"q")
            else:
                os.kill(pid, ending)
            deadline, ended = time.monotonic() + 10, False
            while not ended and time.monotonic() < deadline:
                ended = os.waitpid(pid, os.WNOHANG)[0] == pid
                time.sleep(0.05)
            self.assertTrue(ended)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)

    def test_quitting_undoes_the_highlight(self):
        self.sidebar("q")
        self.assertEqual(self.m.pane_options(1), {})
        self.assertEqual(self.m.window_options(1), {})

    def test_a_hangup_or_terminate_undoes_it_too(self):
        for sig in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(sig=sig):
                self.sidebar(sig)
                self.assertEqual(self.m.pane_options(1), {})
                self.assertEqual(self.m.window_options(1), {})


if __name__ == "__main__":
    unittest.main()
