"""Tests for the tmux key source and the KEYS block."""
import os
import sys
import tempfile
import unittest
from unittest import mock

from menu_fixtures import LIB, Machine
from test_agent_menu import load_script

sys.path.insert(0, str(LIB))
import menu_render  # noqa: E402
import menu_style  # noqa: E402
from sources import keys_src  # noqa: E402

# Trimmed from `tmux list-keys` on byobu 5.133 with tmux-resurrect, tmux 3.4.
BYOBU_ROOT = r"""bind-key    -T root         F2                     new-window -c "#{pane_current_path}" \; rename-window -
bind-key    -T root         C-F2                   display-panes \; split-window -h -c "#{pane_current_path}"
bind-key    -T root         S-F2                   display-panes \; split-window -v -c "#{pane_current_path}"
bind-key    -T root         S-F8                   next-layout
bind-key    -T root         S-F11                  resize-pane -Z
bind-key    -T root         M-S-F8                 new-window "byobu-layout restore; clear; /bin/bash"
bind-key    -T root         C-S-F8                 command-prompt -p "Save byobu layout as:" "run-shell \"byobu-layout save '%%'\""
"""
BYOBU_PREFIX = r"""bind-key    -T prefix       C-r                    run-shell /home/u/.tmux/plugins/tmux-resurrect/scripts/restore.sh
bind-key    -T prefix       C-s                    run-shell /home/u/.tmux/plugins/tmux-resurrect/scripts/save.sh
bind-key    -T prefix       \"                     split-window
bind-key    -T prefix       \%                     split-window -h
bind-key    -T prefix       >                      display-menu -T "#{pane_index}" -x P -y P "Horizontal Split" h { split-window -h } "Vertical Split" v { split-window -v }
bind-key    -T prefix       z                      resize-pane -Z
bind-key    -T prefix       |                      split-window -c "#{pane_current_path}"
bind-key -r -T prefix       M-Up                   resize-pane -U 5
bind-key    -T copy-mode    C-r                    command-prompt -i -I "#{pane_search_string}" -T search -p "(search up)"
"""
# Plain tmux 3.4 defaults: no function keys, no layout saving.
PLAIN = r"""bind-key    -T prefix       Space                  next-layout
bind-key    -T prefix       \"                     split-window
bind-key    -T prefix       \%                     split-window -h
bind-key    -T prefix       z                      resize-pane -Z
"""


class ParseTest(unittest.TestCase):
    def test_byobu_with_resurrect(self):
        keys = keys_src.parse(BYOBU_ROOT + BYOBU_PREFIX, "C-a")
        self.assertEqual(keys, {"split-side": ["C-F2", "C-a %"], "split-stacked": ["S-F2", 'C-a "'],
                                "zoom": ["S-F11", "C-a z"], "next-layout": ["S-F8"],
                                "save-layout": ["C-a C-s"], "restore-layout": ["C-a C-r"]})

    def test_merged_flags_still_split_side_by_side(self):
        for command in ("split-window -bh", "split-window -fh", 'split-window -hb -c "#{pane_current_path}"'):
            self.assertEqual(keys_src.action_of(command), "split-side", command)
        self.assertEqual(keys_src.action_of("split-window -bv"), "split-stacked")
        self.assertEqual(keys_src.action_of('split-window -c "/home/hat"'), "split-stacked")   # an h in a value

    def test_a_menu_that_offers_a_split_is_not_a_split(self):
        self.assertEqual(keys_src.action_of('display-menu -T x "Horizontal Split" h { split-window -h }'), "")
        self.assertEqual(keys_src.action_of("resize-pane -U 5"), "")

    def test_plain_tmux_defaults(self):
        keys = keys_src.parse(PLAIN, "C-b")
        self.assertEqual(keys, {"split-side": ["C-b %"], "split-stacked": ['C-b "'], "zoom": ["C-b z"],
                                "next-layout": ["C-b Space"]})

    def test_byobu_named_layouts_only_without_resurrect(self):
        keys = keys_src.parse(BYOBU_ROOT, "C-a")
        self.assertEqual((keys["save-layout"], keys["restore-layout"]), (["C-S-F8"], ["M-S-F8"]))
        keys = keys_src.parse(BYOBU_ROOT + BYOBU_PREFIX, "C-a")
        self.assertEqual((keys["save-layout"], keys["restore-layout"]), (["C-a C-s"], ["C-a C-r"]))

    def test_no_bindings_no_prefix(self):
        self.assertEqual(keys_src.parse("", ""), {})
        self.assertEqual(keys_src.parse(PLAIN, "")["zoom"], ["prefix z"])

    def test_quoted_and_escaped_key_names(self):
        self.assertEqual(keys_src._key_name('"M-{"'), "M-{")
        self.assertEqual(keys_src._key_name("\\;"), ";")
        self.assertEqual(keys_src._key_name("C-F2"), "C-F2")


class ReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keys")
        self.addCleanup(tmp.cleanup)
        self.m = Machine(tmp.name)
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_asks_the_server(self):
        (self.m.dir / "keys-root.txt").write_text(BYOBU_ROOT)
        (self.m.dir / "keys-prefix.txt").write_text(BYOBU_PREFIX)
        (self.m.dir / "gopt-prefix").write_text("C-a")
        prefix, keys = keys_src.read()
        self.assertEqual(prefix, "C-a")
        self.assertEqual(keys["save-layout"], ["C-a C-s"])

    def test_the_sidebar_remembers_whether_the_block_is_shown(self):
        menu = load_script("agent-menu")
        self.assertTrue(menu.keys_shown())                               # unset: shown
        menu.tmux_src.set_global_option(menu.KEYS_OPTION, "off")
        self.assertEqual((self.m.dir / "gopt-@agent-menu-keys").read_text(), "off")
        self.assertFalse(menu.keys_shown())
        with mock.patch.dict(os.environ, {"STUB_TMUX_FAIL": "1"}):
            self.assertTrue(menu.keys_shown())                           # no tmux: shown, never an error


class KeyRowsTest(unittest.TestCase):
    KEYS = keys_src.parse(BYOBU_ROOT + BYOBU_PREFIX, "C-a")

    def test_block_fits_every_width_it_draws_at(self):
        for columns in (26, 30, 34, 48, 62):
            rows = menu_render.key_rows("C-a", self.KEYS, columns)
            self.assertEqual(len(rows), 2 + 6)
            for row in rows:
                self.assertLessEqual(menu_render.width(row["text"]), columns, (columns, row["text"]))

    def test_layout_at_the_default_width(self):
        texts = [r["text"] for r in menu_render.key_rows("C-a", self.KEYS, 34)]
        self.assertTrue(texts[1].startswith(" KEYS") and texts[1].endswith("prefix C-a "))
        self.assertIn("  split left|right C-F2  C-a %", texts)
        self.assertIn("  restore layout   C-a C-r", texts)

    def test_narrow_pane_keeps_the_key_pressed_alone(self):
        texts = [r["text"] for r in menu_render.key_rows("C-a", self.KEYS, 28)]
        self.assertIn("  split left|right C-F2", texts)

    def test_hidden_when_too_narrow_or_nothing_bound(self):
        self.assertEqual(menu_render.key_rows("C-a", self.KEYS, 25), [])
        self.assertEqual(menu_render.key_rows("C-a", {}, 34), [])

    def test_ascii_form(self):
        for row in menu_render.key_rows("C-a", self.KEYS, 34, menu_style.ASCII):
            self.assertTrue(row["text"].isascii(), row["text"])

    def test_help_names_the_toggle(self):
        self.assertIn("s keys", "\n".join(menu_render.HELP))
        self.assertIn("s keys", "\n".join(menu_render.HELP_ASCII))


if __name__ == "__main__":
    unittest.main()
