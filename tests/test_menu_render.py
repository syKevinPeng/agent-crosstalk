"""Tests for the pure parts: auth detection and sidebar rendering."""
import sys
import unittest

from menu_fixtures import LIB

sys.path.insert(0, str(LIB))
import auth_readers  # noqa: E402
import menu_render  # noqa: E402
import menu_style  # noqa: E402
from agent_state import Agent, Snapshot  # noqa: E402


class AuthReadersTest(unittest.TestCase):
    def test_prompts_at_the_bottom_of_a_screen_are_labelled(self):
        cases = {
            "Enter passphrase for key '/k/id': ": "ssh key passphrase",
            "[sudo] password for someone: ": "sudo password",
            "someone@cluster's password: ": "password",
            "Password for 'https://user@host.example': ": "password",
            "(current) UNIX password: ": "password",
            "Vault password: ": "password",
            "Username for 'https://host.example': ": "username",
            "Enter PIN: ": "credential", "passphrase: ": "password", "Enter your OTP: ": "credential",
            "Token: ": "credential", "Passcode: ": "credential", "Enter MFA code": "second factor",
            "(someone@cluster) Password: ": "password", "Enter your password: ": "password",
            "Password (again): ": "password", "Retype new password: ": "password",
            "BECOME password: ": "password", "SSH password: ": "password",
            "[sudo] Passwort für someone: ": "password", "Bad passphrase, try again for /k/id: ": "password",
            "2FA code: ": "second factor", "Authentication code: ": "second factor",
            "Enter the 6-digit code from your app": "second factor",
            "Then enter the code:": "browser sign-in", "Open the following URL in a browser:": "browser sign-in",
            "Duo two-factor login for someone": "second factor",
            "Passcode or option (1-3): ": "second factor",
            "Verification code: ": "second factor",
            "First copy your one-time code: AB12-CD34": "device sign-in",
            "Please run /login to continue": "claude login",
            "Not signed in. Run `codex login` first": "codex login",
            "To get started, please run:  gh auth login": "github login",
        }
        for screen, label in cases.items():
            with self.subTest(screen=screen):
                self.assertEqual(auth_readers.detect_screen("some earlier output\n" + screen), label)

    def test_a_prompt_is_found_behind_the_cli_chrome_drawn_around_and_under_it(self):
        footer = "\n──────\n❯ \n──────\n  ⏵⏵ auto mode on (shift+tab to cycle)\n  status line\n  another line\n"
        for shown in ("  ⎿  Password: ", "│ Password:", "  └ [sudo] password for someone: ", "• Enter passphrase for key '/k': "):
            with self.subTest(shown=shown):
                self.assertIsNotNone(auth_readers.detect_screen("running...\n" + shown + footer))

    def test_ordinary_screens_mentions_and_old_prompts_are_not_auth(self):
        for screen in ("› Ask Codex to do anything\n", "", "The password: field in the form is validated here.\n",
                       "You can run gh auth login later.", "I suggest gcloud auth login if needed.",
                       "The docs say please run /login when the session expires.", "password policy: strong",
                       "The token: field is parsed and then stored"):
            with self.subTest(screen=screen):
                self.assertIsNone(auth_readers.detect_screen(screen))
        old = "Password: \n" + "\n".join(f"line {i}" for i in range(20))
        self.assertIsNone(auth_readers.detect_screen(old))     # scrolled far up, no longer the live prompt

    def test_auth_failures_in_an_answer_are_labelled(self):
        self.assertEqual(auth_readers.detect_output("git@host: Permission denied (publickey)."), "ssh key rejected")
        self.assertEqual(auth_readers.detect_output("error: 401 Unauthorized"), "http 401")
        self.assertIsNone(auth_readers.detect_output("All 401 tests passed."))


class RenderTest(unittest.TestCase):
    def snap(self, *roots, errors=()):
        by_key = {}

        def walk(agent):
            if agent.key in by_key:
                return                      # the loop test builds a cycle on purpose
            by_key[agent.key] = agent
            for child in agent.children:
                walk(child)
        for root in roots:
            walk(root)
        return Snapshot(roots=list(roots), by_key=by_key, errors=list(errors))

    def agent(self, name, **kw):
        return Agent(key=f"k:{name}", kind=kw.pop("kind", "codex"), name=name, session_id=name, **kw)

    def test_status_priority_and_marks_in_both_glyph_sets(self):
        a = self.agent("x", state="working", open_messages=2, needs_owner=1, auth="password")
        seen = []
        for clear, empty in (("auth", ""), ("needs_owner", 0), ("open_messages", 0), ("state", "idle"), (None, None)):
            seen.append((menu_render.status(a), menu_render.mark(a), menu_render.mark(a, menu_style.ASCII)))
            if clear:
                setattr(a, clear, empty)
        self.assertEqual(seen, [("auth", "✗", "X"), ("needs", "!1", "!1"), ("unanswered", "✉2", "+2"),
                                ("working", "⠋", "*"), ("idle", "✓", ".")])
        self.assertEqual(menu_render.mark(self.agent("y", state="unknown")), "-")
        self.assertEqual(menu_render.mark(a := self.agent("z", state="working"), frame=3), "⠸")   # the spinner turns

    def test_every_glyph_is_one_cell_wide_and_no_emoji(self):
        import unicodedata
        for name, glyph_set in (("unicode", menu_style.UNICODE), ("ascii", menu_style.ASCII)):
            for key, value in glyph_set.items():
                for ch in value:
                    with self.subTest(set=name, glyph=key, char=ch):
                        self.assertNotIn(unicodedata.east_asian_width(ch), ("W", "F"))
                        self.assertLess(ord(ch), 0x1F000)          # the emoji planes
                        self.assertNotEqual(ch, "\ufe0f")           # never force emoji presentation
                        if name == "ascii":
                            self.assertTrue(ch.isascii())

    def test_each_state_has_its_own_shape_and_at_most_three_have_a_colour(self):
        for glyph_set in (menu_style.UNICODE, menu_style.ASCII):
            shapes = [glyph_set["auth"], glyph_set["needs"], glyph_set["unanswered"], glyph_set["spinner"][0],
                      glyph_set["idle"], glyph_set["stopped"]]
            self.assertEqual(len(set(shapes)), 6, shapes)
        colours = [colour for colour, _ in menu_style.LOOK.values() if colour]
        self.assertLessEqual(len(colours), 3)
        self.assertNotIn("blue", colours)

    def test_colour_and_ascii_switches(self):
        self.assertTrue(menu_style.colour_allowed({}, True))
        self.assertFalse(menu_style.colour_allowed({"NO_COLOR": "1"}, True))
        self.assertTrue(menu_style.colour_allowed({"NO_COLOR": ""}, True))        # empty means unset
        self.assertFalse(menu_style.colour_allowed({"TERM": "dumb"}, True))
        self.assertTrue(menu_style.colour_allowed({"NO_COLOR": "1", "FORCE_COLOR": "1"}, True))
        self.assertFalse(menu_style.colour_allowed({}, False))
        self.assertTrue(menu_style.use_ascii({}, "ANSI_X3.4-1968"))               # a non-UTF-8 locale
        self.assertFalse(menu_style.use_ascii({}, "UTF-8"))
        self.assertTrue(menu_style.use_ascii({"AGENT_MENU_ASCII": "1"}, "UTF-8"))
        self.assertFalse(menu_style.animate({"AGENT_MENU_NO_ANIMATION": "1"}))

    def test_every_row_fits_ends_with_its_mark_and_long_names_are_cut(self):
        child = self.agent("a-very-long-child-name-that-cannot-fit", access="rw", auth="password")
        root = self.agent("an extremely long parent session name", kind="claude", children=[child])
        for columns in (24, 34, 60):
            for glyph_set in (menu_style.UNICODE, menu_style.ASCII):
                for row in menu_render.rows(self.snap(root), columns, glyphs=glyph_set):
                    self.assertLessEqual(menu_render.width(row["text"]), columns, row["text"])
                    self.assertTrue(row["text"].endswith(row["mark"]))   # the drawing layer colours this tail
        rendered = menu_render.rows(self.snap(root), 34)
        self.assertIn("…", rendered[1]["text"])
        self.assertTrue(rendered[2]["text"].endswith("rw codex ✗"))
        self.assertEqual(rendered[2]["look"], "auth")

    def test_a_retired_row_says_so_in_words(self):
        gone = self.agent("done-review", access="ro", retired=True, state="stopped")
        row = menu_render.rows(self.snap(self.agent("root", children=[gone])), 34)[2]
        self.assertEqual(row["style"], "retired")
        self.assertTrue(row["text"].endswith("retired -"), row["text"])
        self.assertEqual(row["mark"], "-")

    def test_folding_hides_children_and_rows_map_back_to_agents(self):
        child = self.agent("child")
        root = self.agent("root", children=[child])
        rendered = menu_render.rows(self.snap(root, errors=["codex: unreachable"]), 34)
        self.assertEqual([row["style"] for row in rendered], ["header", "error", "row", "row"])
        self.assertEqual(menu_render.key_at(rendered, 0), "")
        self.assertEqual(menu_render.key_at(rendered, 2), "k:root")
        self.assertEqual(menu_render.key_at(rendered, 3), "k:child")
        self.assertEqual(menu_render.key_at(rendered, 99), "")
        folded = menu_render.rows(self.snap(root), 34, collapsed={"k:root"})
        self.assertEqual(len(folded), 2)
        self.assertTrue(folded[1]["text"].startswith(" ▸ root"))

    def test_tree_keys_follow_the_aria_pattern(self):
        kid1, kid2 = self.agent("kid1", parent_key="k:root"), self.agent("kid2", parent_key="k:root")
        root, other = self.agent("root", children=[kid1, kid2]), self.agent("other")
        snap = self.snap(root, other)

        def step(selected, folded, action):
            return menu_render.move(snap, folded, selected, action)
        self.assertEqual(step("k:root", set(), "right"), ("k:kid1", set()))              # open parent: first child
        self.assertEqual(step("k:root", {"k:root"}, "right"), ("k:root", set()))         # closed parent: open it
        self.assertEqual(step("k:root", set(), "left"), ("k:root", {"k:root"}))          # open parent: close it
        self.assertEqual(step("k:kid2", set(), "left"), ("k:root", set()))               # child: go to parent
        self.assertEqual(step("k:root", {"k:root"}, "down"), ("k:other", {"k:root"}))    # folded children are skipped
        self.assertEqual(step("k:kid1", set(), "end"), ("k:other", set()))
        self.assertEqual(step("k:other", set(), "home"), ("k:root", set()))
        self.assertEqual(step("k:gone", set(), "down"), ("k:root", set()))               # a vanished selection resets
        self.assertEqual(step("k:other", set(), "toggle"), ("k:other", set()))           # a leaf does not fold

    def test_help_fits_the_sidebar_without_being_cut(self):
        for line in menu_render.HELP:
            self.assertLessEqual(menu_render.width(line), 32, line)

    def test_any_depth_is_drawn_and_a_loop_in_the_records_draws_each_agent_once(self):
        d = self.agent("level3")
        c = self.agent("level2", children=[d])
        b = self.agent("level1", children=[c])
        a = self.agent("level0", children=[b])
        names = [row["text"].split()[1] if row["key"] else "" for row in menu_render.rows(self.snap(a), 40)]
        self.assertEqual([n for n in names if n], ["level0", "level1", "level2", "level3"])
        d.children.append(a)                                   # a forged loop: level3 "made" level0
        looped = menu_render.rows(self.snap(a), 40)
        self.assertEqual(len([r for r in looped if r["key"]]), 4)
        for row in looped:
            self.assertLessEqual(menu_render.width(row["text"]), 40)

    def test_a_deep_tree_still_fits_a_narrow_pane(self):
        agent = self.agent("level0")
        root = agent
        for depth in range(1, 9):
            child = self.agent(f"level{depth}", access="ro")
            agent.children.append(child)
            agent = child
        for columns in (20, 24, 27, 34):
            for row in menu_render.rows(self.snap(root), columns):
                self.assertLessEqual(menu_render.width(row["text"]), columns, (columns, row["text"]))

    def test_the_selected_row_is_always_on_screen(self):
        agents = [self.agent(f"agent{i:03}") for i in range(200)]
        rendered = menu_render.rows(self.snap(*agents, errors=["codex: unreachable"]), 34)
        keys = [row["key"] for row in rendered if row["key"]]
        top = 0
        for wanted in (keys[0], keys[150], keys[-1], keys[3], keys[60]):
            top = menu_render.scroll_top(rendered, wanted, 10, top)
            self.assertIn(wanted, keys[top:top + 10])
            self.assertTrue(0 <= top <= len(keys) - 10)
        self.assertEqual(menu_render.scroll_top(rendered, "k:gone", 10, 50), 0)
        self.assertEqual(menu_render.scroll_top(rendered[:5], keys[1], 10, 7), 0)     # everything fits: no scroll

    def test_a_combining_mark_takes_no_cell_so_the_mark_is_not_drawn_twice(self):
        self.assertEqual(menu_render.width("e\u0301"), 1)
        self.assertEqual(menu_render.width("a\ufe0fb\u200d"), 2)       # a variation selector and a joiner take no cell
        row = menu_render.rows(self.snap(self.agent("cafe\u0301 session", state="idle")), 34)[1]
        self.assertEqual(menu_render.width(row["text"]), 33)
        self.assertEqual(row["text"].count("✓"), 1)

    def test_a_retired_agent_shows_only_the_stopped_mark(self):
        gone = self.agent("done", retired=True, state="idle", open_messages=2, needs_owner=1, auth="password")
        self.assertEqual((menu_render.status(gone), menu_render.mark(gone)), ("stopped", "-"))

    def test_help_and_footer_have_ascii_forms(self):
        for line in menu_render.HELP_ASCII + [menu_render.footer(34, glyphs=menu_style.ASCII)]:
            self.assertTrue(line.isascii(), line)
            self.assertLessEqual(len(line), 32)
        self.assertLessEqual(len(menu_render.HELP), 12)
        self.assertLessEqual(len(menu_render.HELP_ASCII), 12)

    def test_footer_has_a_short_form_and_too_small_is_detected(self):
        self.assertIn("? help", menu_render.footer(34))
        self.assertLessEqual(menu_render.width(menu_render.footer(18)), 18)
        self.assertEqual(menu_render.footer(34, "pane focused"), " pane focused")
        self.assertTrue(menu_render.too_small(3, 34))
        self.assertTrue(menu_render.too_small(40, 19))
        self.assertFalse(menu_render.too_small(40, 20))          # exactly the minimum is enough
        self.assertFalse(menu_render.too_small(40, 34))


if __name__ == "__main__":
    unittest.main()
