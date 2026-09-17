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

    def test_ordinary_screens_and_old_prompts_are_not_auth(self):
        self.assertIsNone(auth_readers.detect_screen("› Ask Codex to do anything\n"))
        self.assertIsNone(auth_readers.detect_screen("The password: field in the form is validated here.\n"))
        old = "Password: \n" + "\n".join(f"line {i}" for i in range(10))
        self.assertIsNone(auth_readers.detect_screen(old))     # scrolled up, no longer the live prompt
        self.assertIsNone(auth_readers.detect_screen(""))

    def test_auth_failures_in_an_answer_are_labelled(self):
        self.assertEqual(auth_readers.detect_output("git@host: Permission denied (publickey)."), "ssh key rejected")
        self.assertEqual(auth_readers.detect_output("error: 401 Unauthorized"), "http 401")
        self.assertIsNone(auth_readers.detect_output("All 401 tests passed."))


class RenderTest(unittest.TestCase):
    def snap(self, *roots, errors=()):
        by_key = {}

        def walk(agent):
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

    def test_footer_has_a_short_form_and_too_small_is_detected(self):
        self.assertIn("? help", menu_render.footer(34))
        self.assertLessEqual(menu_render.width(menu_render.footer(18)), 18)
        self.assertEqual(menu_render.footer(34, "pane focused"), " pane focused")
        self.assertTrue(menu_render.too_small(3, 34))
        self.assertTrue(menu_render.too_small(40, 12))
        self.assertFalse(menu_render.too_small(40, 34))


if __name__ == "__main__":
    unittest.main()
