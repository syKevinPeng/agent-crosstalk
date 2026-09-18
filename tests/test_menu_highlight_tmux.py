"""The pane highlight against a real tmux, on a private server each test starts and kills, with its
socket in the test's own temp folder. The stub tmux cannot say how tmux expands a style, so this asks
tmux itself: `display -p -t <pane> <style>` expands a style the way tmux does when it draws that
pane's border. Skipped where tmux is missing."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from menu_fixtures import LIB

sys.path.insert(0, str(LIB))
import menu_highlight  # noqa: E402

# Siyuan's global styles, tmux 3.4's own defaults, and styles with commas, nested formats and `#x`.
STYLES = [
    ("fg=#75507B", "bg=#DD4814,fg=#DD4814"),
    (None, None),
    ("fg=blue,bold", "#{?pane_active,fg=red,fg=blue},bold"),
    ("fg=#DD4814,#{?pane_active,bold,dim}", "#{?#{==:#{pane_index},1},fg=red,fg=blue}"),
]


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class RealTmuxHighlightTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hlt")
        self.addCleanup(self._tmp.cleanup)
        self.socket = os.path.join(self._tmp.name, "sock")    # never the owner's server, never shared
        wrapper = os.path.join(self._tmp.name, "tmux")
        with open(wrapper, "w") as fh:
            fh.write(f'#!/bin/sh\nexec tmux -S {self.socket} -f /dev/null "$@"\n')
        os.chmod(wrapper, 0o755)
        patcher = mock.patch.dict(os.environ, {"TMUX_BIN": wrapper})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmux, "kill-server")
        self.tmux("new-session", "-d", "-s", "t", "-x", "120", "-y", "30", "sleep 120")
        self.tmux("split-window", "-h", "-t", "t", "sleep 120")
        self.tmux("split-window", "-v", "-t", "t", "sleep 120")
        self.panes = self.tmux("list-panes", "-t", "t", "-F", "#{pane_id}").split()
        self.window = self.tmux("display-message", "-p", "-t", "t", "#{window_id}")

    def tmux(self, *args):
        """The private server only: every call names it, so the owner's own tmux is never touched."""
        return subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", *args],
                              capture_output=True, text=True, timeout=10).stdout.rstrip("\n")

    def own(self):
        return ([self.tmux("show-options", "-p", "-t", p) for p in self.panes],
                self.tmux("show-options", "-w", "-t", self.window))

    def looks(self, name):
        """How tmux draws `name` for each pane: the window's style, expanded with that pane's options."""
        style = self.tmux("show-options", "-w", "-A", "-v", "-t", self.window, name)
        return [self.tmux("display-message", "-p", "-t", p, style) if "#{" in style else style for p in self.panes]

    def test_only_the_marked_pane_looks_different_and_all_comes_back(self):
        look = menu_highlight.styles()
        for border, active in STYLES:
            with self.subTest(border=border, active=active):
                for name, value in (("pane-border-style", border), ("pane-active-border-style", active)):
                    self.tmux("set-option", "-g", *(["-u", name] if value is None else [name, value]))
                before_own = self.own()
                before = {name: self.looks(name) for name in menu_highlight.BORDERS}
                light = menu_highlight.Highlighter()
                light.show(self.panes[0])
                for name in menu_highlight.BORDERS:
                    during = self.looks(name)
                    self.assertEqual(during[0], look[name], name)                # the marked pane
                    self.assertEqual(during[1:], before[name][1:], name)         # every other pane as before
                self.assertEqual(self.tmux("show-options", "-p", "-v", "-t", self.panes[0], "window-style"),
                                 look["window-style"])
                light.show(self.panes[1])                                          # a move within the window
                self.assertEqual(self.looks("pane-border-style")[1], look["pane-border-style"])
                light.clear()
                self.assertEqual(self.own(), before_own)

    def test_what_the_owner_sets_during_a_highlight_stays(self):
        light = menu_highlight.Highlighter()
        light.show(self.panes[0])
        self.tmux("set-option", "-w", "-t", self.window, "pane-border-style", "fg=red")
        self.tmux("select-pane", "-t", self.panes[0], "-P", "bg=blue")     # sets both pane styles
        light.clear()
        self.assertEqual(self.tmux("show-options", "-w", "-v", "-t", self.window, "pane-border-style"), "fg=red")
        for name in menu_highlight.TINTS:
            self.assertEqual(self.tmux("show-options", "-p", "-v", "-t", self.panes[0], name), "bg=blue")
        self.assertEqual(self.tmux("show-options", "-w", "-v", "-t", self.window, "pane-active-border-style"), "")
        self.assertEqual(self.tmux("show-options", "-p", "-t", self.panes[0], menu_highlight.MARK), "")

    def test_what_a_killed_sidebar_left_is_undone_by_the_next(self):
        before = self.own()
        menu_highlight.Highlighter().show(self.panes[2])                           # never cleared
        self.assertNotEqual(self.own(), before)
        menu_highlight.clear_stale()
        self.assertEqual(self.own(), before)


if __name__ == "__main__":
    unittest.main()
