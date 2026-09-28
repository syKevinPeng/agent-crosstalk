"""bin/agent-menu-toggle and agent-crosstalk.tmux against a real tmux, on a private server each test
starts and kills, with its socket in the test's own temp folder. The sidebar it opens is a stand-in
script named bin/agent-menu, so no test reads a real agent. Skipped where tmux is missing."""
import os
import shutil
import subprocess
import tempfile
import unittest

from menu_fixtures import BIN

ROOT = BIN.parent
TOGGLE = BIN / "agent-menu-toggle"


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class RealTmuxToggleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tgl")
        self.addCleanup(self._tmp.cleanup)
        self.socket = os.path.join(self._tmp.name, "sock")    # never the owner's server, never shared
        self.wrapper = os.path.join(self._tmp.name, "tmux")
        with open(self.wrapper, "w") as fh:
            fh.write(f'#!/bin/sh\nexec tmux -S {self.socket} -f /dev/null "$@"\n')
        os.chmod(self.wrapper, 0o755)
        fake_bin = os.path.join(self._tmp.name, "bin")
        os.mkdir(fake_bin)
        self.fake_menu = os.path.join(fake_bin, "agent-menu")
        with open(self.fake_menu, "w") as fh:
            fh.write("#!/bin/sh\nexec sleep 120\n")
        os.chmod(self.fake_menu, 0o755)
        self.addCleanup(self.tmux, "kill-server")
        self.tmux("new-session", "-d", "-s", "t", "-x", "160", "-y", "40", "sleep 120")
        self.tmux("split-window", "-h", "-t", "t", "sleep 120")
        self.work = self.tmux("list-panes", "-t", "t", "-F", "#{pane_id}").split()

    def tmux(self, *args):
        """The private server only: every call names it, so the owner's own tmux is never touched."""
        return subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", *args],
                              capture_output=True, text=True, timeout=10).stdout.rstrip("\n")

    def toggle(self, pane):
        proc = subprocess.run([str(TOGGLE), pane], capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, TMUX_BIN=self.wrapper, AGENT_MENU_BIN=self.fake_menu))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def panes(self):
        """{pane id: (left, width, height, active, tag)} for the window."""
        rows = self.tmux("list-panes", "-t", "t", "-F",
                         "#{pane_id} #{pane_left} #{pane_width} #{pane_height} #{pane_active} #{@agent-menu}")
        return {r.split()[0]: tuple(r.split()[1:]) for r in rows.splitlines()}

    def sidebar(self):
        found = [p for p in self.panes() if p not in self.work]
        self.assertEqual(len(found), 1, self.panes())
        return found[0]

    def test_open_focus_close(self):
        self.toggle(self.work[1])
        menu = self.sidebar()
        left, width, height, _, tag = self.panes()[menu]
        self.assertEqual((left, width, tag), ("0", "34", "1"))                   # full-height, left edge
        self.assertEqual(height, self.panes()[self.work[0]][2])
        self.tmux("select-pane", "-t", self.work[1])
        self.toggle(self.work[1])                                                # from another pane: focus it
        self.assertEqual(len(self.panes()), 3)
        self.assertEqual(self.panes()[menu][3], "1")
        self.toggle(menu)                                                        # from the sidebar: close it
        self.assertNotIn(menu, self.panes())
        self.assertEqual(len(self.panes()), 2)

    def test_a_sidebar_started_by_hand_is_found_not_doubled(self):
        self.tmux("split-window", "-hbf", "-l", "40", "-t", self.work[0], f"{self.fake_menu}; echo done")
        by_hand = self.sidebar()
        self.toggle(self.work[0])
        self.assertEqual(len(self.panes()), 3)
        self.assertEqual(self.panes()[by_hand][3], "1")

    def test_another_tools_pane_is_not_taken_for_the_sidebar(self):
        self.tmux("split-window", "-h", "-t", self.work[0], f"{self.fake_menu}-detail; sleep 120")
        self.tmux("select-pane", "-t", self.work[0], "-T", "agent-sidebar")
        self.work = list(self.panes())
        self.toggle(self.work[0])
        self.assertEqual(self.panes()[self.sidebar()][4], "1")                  # a new, tagged sidebar

    def test_the_width_option_is_used_and_a_bad_one_ignored(self):
        self.tmux("set-option", "-g", "@agent-menu-width", "50")
        self.toggle(self.work[0])
        self.assertEqual(self.panes()[self.sidebar()][1], "50")
        self.toggle(self.sidebar())
        self.tmux("set-option", "-g", "@agent-menu-width", "wide; rm -rf x")
        self.toggle(self.work[0])
        self.assertEqual(self.panes()[self.sidebar()][1], "34")

    def test_only_its_own_window_is_searched(self):
        self.toggle(self.work[0])
        self.tmux("new-window", "-t", "t", "sleep 120")
        other = self.tmux("display-message", "-p", "-t", "t:1", "#{pane_id}")
        self.toggle(other)
        self.assertEqual(self.tmux("list-panes", "-t", "t:1", "-F", "#{@agent-menu}").split(), ["1"])

    def test_the_plugin_file_binds_the_key_and_honours_the_option(self):
        run = dict(os.environ, TMUX_BIN=self.wrapper)
        subprocess.run([str(ROOT / "agent-crosstalk.tmux")], env=run, check=True, timeout=10)
        self.assertIn(str(TOGGLE), self.tmux("list-keys", "-T", "prefix", "O"))
        self.tmux("set-option", "-g", "@agent-menu-key", "g")
        subprocess.run([str(ROOT / "agent-crosstalk.tmux")], env=run, check=True, timeout=10)
        self.assertIn(str(TOGGLE), self.tmux("list-keys", "-T", "prefix", "g"))

    def test_outside_tmux_without_a_pane_it_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        proc = subprocess.run([str(TOGGLE)], capture_output=True, text=True, timeout=10,
                              env=dict(env, TMUX_BIN=self.wrapper))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("pane", proc.stderr)


if __name__ == "__main__":
    unittest.main()
