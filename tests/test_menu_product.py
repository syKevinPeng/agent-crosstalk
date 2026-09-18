"""The agent menu as a product someone installs from GitHub: light themes, older tmux, a slow or absent
CLI, states that do not mislead, keys that say why they did nothing. Stubs and fakes only."""
import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from menu_fixtures import LIB, Machine

sys.path.insert(0, str(LIB))
import agent_state  # noqa: E402
import menu_highlight  # noqa: E402
import menu_popup  # noqa: E402
import menu_render  # noqa: E402
import menu_style  # noqa: E402
from sources import claude_src, limits_src, tmux_src  # noqa: E402
from sources.base import SourceError  # noqa: E402

CLAUDE_A = "aaaaaaaa-0000-4000-8000-000000000001"


class MachineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mp")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class LightThemeTest(unittest.TestCase):
    def test_the_tint_sets_a_text_colour_too(self):
        # On a light theme a dark tint under the theme's dark text would be unreadable.
        look = menu_highlight.styles()
        self.assertEqual(look["window-style"], "bg=colour236,fg=colour252")
        with mock.patch.dict(os.environ, {"AGENT_MENU_HIGHLIGHT_FG": "black", "AGENT_MENU_HIGHLIGHT_BG": "colour254"}):
            self.assertEqual(menu_highlight.styles()["window-style"], "bg=colour254,fg=black")


class OlderTmuxTest(MachineTest):
    def test_a_popup_falls_back_when_tmux_has_no_border_or_title_flags(self):
        # display-popup -b and -T came with tmux 3.3. Ubuntu 22.04 ships 3.2a.
        with mock.patch.object(tmux_src, "_tmux", wraps=tmux_src._tmux) as call:
            def older(*args, **kw):
                if args[0] == "display-popup" and "-b" in args:
                    raise SourceError("tmux display-popup: unknown flag -b")
                return "" if args[0] == "display-popup" else "200 50\n"
            call.side_effect = older
            tmux_src.popup("true", title="agent")
        popups = [c.args for c in call.call_args_list if c.args[0] == "display-popup"]
        self.assertEqual(len(popups), 2)
        self.assertNotIn("-b", popups[1])
        self.assertNotIn("-T", popups[1])


class SlowOrMissingCliTest(MachineTest):
    def test_a_listing_that_times_out_says_so(self):
        slow = self.m.dir / "slow-claude"
        slow.write_text("#!/usr/bin/env bash\nsleep 3\n")
        slow.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(slow)}):
            with self.assertRaises(SourceError) as caught:
                claude_src._listing(timeout=0.3)
        self.assertIn("timed out", str(caught.exception))

    def test_the_listing_waits_longer_than_a_slow_start(self):
        self.assertGreaterEqual(claude_src.LIST_TIMEOUT, 5)

    def test_a_listing_in_another_shape_is_an_error_not_an_empty_tree(self):
        odd = self.m.dir / "odd-claude"
        odd.write_text('#!/usr/bin/env bash\necho \'{"sessions": []}\'\n')
        odd.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(odd)}):
            with self.assertRaises(SourceError):
                claude_src._listing()

    def test_a_cli_that_is_not_installed_is_no_error(self):
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": str(self.m.dir / "no-such-claude"),
                                          "CODEX_BIN": str(self.m.dir / "no-such-codex"),
                                          "CODEX_APP_SERVER_SOCK": str(self.m.dir / "no-such.sock")}):
            snapshot = agent_state.collect()
        self.assertEqual([e for e in snapshot.errors if e.startswith(("claude", "codex"))], [])

    def test_the_selection_waits_for_a_row_that_vanished_for_a_refresh(self):
        self.m.claude_session(CLAUDE_A, "lead", 110, status="busy")
        with_row = agent_state.collect()
        key = f"claude:{CLAUDE_A}"
        self.assertIn(key, with_row.by_key)
        self.m.claude = []
        self.m.write()
        without = agent_state.collect()
        selected, _ = menu_render.move(without, set(), key, "stay")
        self.assertEqual(selected, key)                      # not dragged to the first row


class HonestStateTest(unittest.TestCase):
    def test_a_running_session_with_no_status_is_not_shown_as_stopped(self):
        agent = agent_state.Agent(key="claude:x", kind="claude", name="x", session_id="x", state="unknown", pid=5)
        self.assertEqual(menu_render.status(agent), "running")
        self.assertEqual(menu_render.mark(agent), menu_style.UNICODE["running"])
        self.assertEqual(menu_render.mark(agent, menu_style.ASCII), "?")

    def test_a_stopped_session_is_still_stopped(self):
        agent = agent_state.Agent(key="claude:x", kind="claude", name="x", session_id="x", state="stopped")
        self.assertEqual(menu_render.status(agent), "stopped")


class PopupKeysTest(unittest.TestCase):
    def type_fast(self, text, start=0.0, gap=0.03):
        state, now = menu_popup.State(typing=True), start
        for ch in text:
            state, _ = menu_popup.step(state, ["Send"], ("char", ch), now=now)
            now += gap
        return state, now - gap

    def test_an_enter_held_back_says_why_and_does_not_restart_the_wait(self):
        state, last = self.type_fast("fix it")
        state, action = menu_popup.step(state, ["Send"], ("key", "enter"), now=last + 0.2)
        self.assertEqual(action, "")
        self.assertIn("pasted", state.held)
        state, action = menu_popup.step(state, ["Send"], ("key", "enter"), now=last + menu_popup.SETTLE + 0.05)
        self.assertEqual(action, "Send")

    def test_a_pasted_blank_line_still_sends_nothing(self):
        state, last = self.type_fast("fix it\n\n".replace("\n", ""))
        state, action = menu_popup.step(state, ["Send"], ("key", "enter"), now=last + 0.01)
        state, action2 = menu_popup.step(state, ["Send"], ("key", "enter"), now=last + 0.02)
        self.assertEqual((action, action2), ("", ""))

    def test_a_confirm_too_soon_says_so(self):
        state, _ = menu_popup.step(menu_popup.State(), ["Quit agent"], ("char", "x"), now=1.0)
        state, action = menu_popup.step(state, ["Quit agent"], ("char", "y"), now=1.2)
        self.assertEqual(action, "")
        self.assertIn("too quick", state.held)


class LocaleTest(unittest.TestCase):
    def test_a_c_locale_gets_ascii_even_in_python_utf8_mode(self):
        self.assertTrue(menu_style.use_ascii({"LANG": "C"}, "utf-8"))
        self.assertTrue(menu_style.use_ascii({"LC_ALL": "POSIX"}, "utf-8"))
        self.assertFalse(menu_style.use_ascii({"LANG": "en_US.UTF-8"}, "utf-8"))
        self.assertFalse(menu_style.use_ascii({"LC_ALL": "C.UTF-8"}, "utf-8"))

    def test_reset_times_do_not_depend_on_the_locale(self):
        now = datetime.datetime(2026, 9, 18, 12, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(limits_src.datetime, "datetime", wraps=datetime.datetime) as dt:
            dt.strptime.side_effect = ValueError("month names in another language")
            seconds = limits_src.parse_reset("Sep 24, 1pm (UTC)", now=now)
        self.assertEqual(seconds, (datetime.datetime(2026, 9, 24, 13, 0, tzinfo=datetime.timezone.utc) - now).total_seconds())


class HelpTest(unittest.TestCase):
    def test_help_lists_every_key_and_the_marks(self):
        text = "\n".join(menu_render.HELP)
        for key in ("g", "G", "h", "l", "w", "u", "a", "mouse"):
            self.assertIn(key, text)
        for glyph in ("✗", "!", "✉", "✓", "?", "-"):
            self.assertIn(glyph, text)


if __name__ == "__main__":
    unittest.main()
