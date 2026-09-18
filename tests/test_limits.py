"""Tests for the rate-limit source and the LIMITS block."""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

from menu_fixtures import LIB, USAGE_TEXT, FakeCodexDaemon, Machine

sys.path.insert(0, str(LIB))
import menu_render  # noqa: E402
import menu_style  # noqa: E402
from sources import limits_src  # noqa: E402
from sources.base import SourceError  # noqa: E402

NOW = datetime.datetime(2026, 9, 17, 22, 30, tzinfo=datetime.timezone.utc)


class LimitsSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lim")
        self.addCleanup(self._tmp.cleanup)
        self.m = Machine(self._tmp.name)
        self.m.env["AGENT_MENU_USAGE_CWD"] = str(self.m.dir / "usage-calls")
        patcher = mock.patch.dict(os.environ, self.m.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_usage_text_is_read_line_by_line(self):
        limits = limits_src.claude_limits(now=NOW)
        self.assertEqual([(x["window"], x["model"], x["percent"]) for x in limits],
                         [("session", "", 18), ("week", "", 11), ("week", "Fable", 16)])
        self.assertEqual(menu_render.short_reset(limits[0]["resets_in"]), "1h")
        self.assertEqual(menu_render.short_reset(limits[2]["resets_in"]), "6d")
        self.assertTrue(os.path.isdir(os.environ["AGENT_MENU_USAGE_CWD"]))   # its transcripts stay in one corner

    def test_a_line_that_does_not_match_is_dropped_not_guessed(self):
        self.m.usage(USAGE_TEXT.replace("Current week (Fable): 16% used", "Weekly cap for Fable is 16 percent"))
        limits = limits_src.claude_limits(now=NOW)
        self.assertEqual([x["model"] for x in limits], ["", ""])             # the Fable line is simply absent
        self.m.usage("Some entirely new wording with no percentages at all.")
        with self.assertRaises(SourceError):
            limits_src.claude_limits(now=NOW)                                # nothing recognised: say so

    def test_a_missing_or_broken_claude_call_is_a_source_error(self):
        for extra in ({"STUB_USAGE_FAIL": "1"}, {"STUB_USAGE_GARBAGE": "1"}, {"CLAUDE_BIN": "/nonexistent"}):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, extra):
                with self.assertRaises(SourceError):
                    limits_src.claude_limits(now=NOW)

    def test_a_reset_time_that_cannot_be_read_leaves_the_percent_alone(self):
        self.m.usage("Current session: 42% used · resets whenever it feels like it\n")
        (limit,) = limits_src.claude_limits(now=NOW)
        self.assertEqual((limit["percent"], limit["resets_in"]), (42, None))

    def test_codex_windows_are_read_from_the_daemon(self):
        daemon = FakeCodexDaemon(self.m.sock)
        daemon.account = {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 76.4, "windowDurationMins": 10080, "resetsAt": NOW.timestamp() + 4 * 86400},
            "secondary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": NOW.timestamp() + 3600}}}}
        limits = limits_src.codex_limits(now=NOW)
        self.assertEqual([(x["source"], x["window"], x["percent"]) for x in limits],
                         [("codex", "week", 76), ("codex", "5h", 5)])
        self.assertEqual(menu_render.short_reset(limits[0]["resets_in"]), "4d")
        daemon.account = {"rateLimits": None, "rateLimitsByLimitId": {}}
        with self.assertRaises(SourceError):
            limits_src.codex_limits(now=NOW)

    def test_one_source_failing_never_hides_the_other(self):
        FakeCodexDaemon(self.m.sock).account = {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 76, "windowDurationMins": 10080, "resetsAt": NOW.timestamp()}}}}
        with mock.patch.dict(os.environ, {"STUB_USAGE_FAIL": "1"}):
            limits, errors = limits_src.read_all(now=NOW)
        self.assertEqual([x["source"] for x in limits], ["codex"])
        self.assertEqual(len(errors), 1)
        self.assertIn("claude", errors[0])

    def test_window_names_and_short_deltas(self):
        self.assertEqual([limits_src.window_name(m) for m in (300, 10080, 1440, 20160, 0)],
                         ["5h", "week", "1d", "2wk", ""])
        self.assertEqual([menu_render.short_reset(s) for s in (None, -5, 30, 600, 7200, 200000)],
                         ["", "now", "now", "10m", "2h", "2d"])


class LimitsBlockTest(unittest.TestCase):
    LIMITS = [{"source": "claude", "window": "session", "model": "", "percent": 18, "resets_in": 3900},
              {"source": "claude", "window": "week", "model": "", "percent": 11, "resets_in": 540000},
              {"source": "claude", "window": "week", "model": "Fable", "percent": 16, "resets_in": 540000},
              {"source": "codex", "window": "week", "model": "", "percent": 94, "resets_in": 380000}]

    def rows(self, columns, **kw):
        return menu_render.limit_rows(self.LIMITS, columns, kw.pop("glyphs", None), kw.pop("taken_at", 1000.0),
                                      kw.pop("now", 1100.0), kw.pop("errors", ()))

    def test_every_row_fills_the_width_in_both_glyph_sets(self):
        for columns in (26, 34, 40, 62):
            for glyphs in (menu_style.UNICODE, menu_style.ASCII):
                for row in self.rows(columns, glyphs=glyphs):
                    self.assertEqual(menu_render.width(row["text"]), columns, (columns, row["text"]))

    def test_the_model_line_sits_under_its_window(self):
        texts = [r["text"] for r in self.rows(34)]
        self.assertTrue(texts[3].strip().startswith("claude week"), texts)
        self.assertIn("Fable", texts[4])
        self.assertTrue(texts[4].startswith("   "), texts[4])     # indented under it

    def test_a_loud_limit_is_marked_and_a_quiet_one_is_not(self):
        looks = [r["look"] for r in self.rows(34)]
        self.assertEqual(looks.count("auth"), 1)                  # only the 94% row
        self.assertEqual([r["look"] for r in self.rows(34)][-1], "auth")

    def test_some_use_never_looks_like_none(self):
        self.assertEqual(menu_render.bar(0, 6, menu_style.ASCII), "......")
        self.assertEqual(menu_render.bar(1, 6, menu_style.ASCII), "#.....")
        self.assertEqual(menu_render.bar(100, 6, menu_style.ASCII), "######")
        self.assertEqual(menu_render.bar(150, 6, menu_style.ASCII), "######")

    def test_an_old_reading_says_so(self):
        self.assertIn("as of", self.rows(34)[1]["text"])
        self.assertIn("stale", self.rows(34, taken_at=1000.0, now=1000.0 + 3600)[1]["text"])

    def test_a_narrow_pane_drops_the_block_and_errors_are_shown(self):
        self.assertEqual(self.rows(24), [])
        rows = menu_render.limit_rows([], 34, errors=["claude: exit 3"])
        self.assertEqual(rows[-1]["look"], "auth")
        self.assertIn("claude: exit 3", rows[-1]["text"])
        self.assertEqual(menu_render.limit_rows([], 34), [])


class TierTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tier")
        self.addCleanup(self._tmp.cleanup)
        import agent_state
        self.home = os.path.expanduser("~")          # short_folder shortens the real home to ~
        kid = agent_state.Agent(key="codex:k", kind="codex", name="code-review", session_id="k", access="ro",
                                cwd=os.path.join(self.home, "work", "project-one"), model="gpt-6-astra",
                                open_messages=1, parent_key="claude:p")
        parent = agent_state.Agent(key="claude:p", kind="claude", name="lead session", session_id="p",
                                   cwd=os.path.join(self.home, "work"), needs_owner=1, children=[kid])
        # A one-cell mark next to two-cell ones: the columns after it must still line up.
        quiet = agent_state.Agent(key="codex:q", kind="codex", name="quiet-one", session_id="q", state="idle",
                                  cwd=os.path.join(self.home, "work", "notes"), model="gpt-6-astra")
        self.snap = agent_state.Snapshot(roots=[parent, quiet],
                                         by_key={"claude:p": parent, "codex:k": kid, "codex:q": quiet}, errors=[])

    def test_each_width_picks_its_layout(self):
        self.assertEqual([menu_render.tier_for(c) for c in (19, 25, 26, 47, 48, 80)],
                         ["narrow", "narrow", "normal", "normal", "wide", "wide"])

    def test_narrow_drops_the_kind_and_access_columns(self):
        texts = [r["text"] for r in menu_render.rows(self.snap, 20) if r["key"]]
        self.assertTrue(all("codex" not in t and "claude" not in t for t in texts), texts)
        self.assertTrue(any("ro" not in t for t in texts))
        self.assertIn("lead", texts[0])
        self.assertTrue(texts[0].rstrip().endswith("!1"))

    def test_normal_keeps_kind_and_access(self):
        texts = [r["text"] for r in menu_render.rows(self.snap, 34) if r["key"]]
        self.assertIn("claude", texts[0])
        self.assertIn("ro codex", texts[1])
        self.assertNotIn("gpt-6-astra", " ".join(texts))

    def test_wide_adds_the_folder_and_the_model(self):
        rows = [r for r in menu_render.rows(self.snap, 62) if r["key"]]
        texts = [r["text"] for r in rows]
        self.assertIn("~/work", texts[0])
        self.assertIn("gpt-6-astra", texts[1])
        self.assertNotIn("gpt", texts[0])                        # Claude reports no model: the cell stays empty
        for row in rows:
            self.assertLessEqual(menu_render.width(row["text"]), 62)
        self.assertEqual(len(texts), 3, texts)
        self.assertEqual(len({t.index("~/") for t in texts}), 1, texts)   # the folder column lines up
        self.assertEqual(len({menu_render.width(t) for t in texts}), 1, texts)

    def test_a_folder_keeps_its_root_while_it_fits(self):
        self.assertEqual(menu_render.short_folder(os.path.join(self.home, "work/project-one"), 16, "…"),
                         "~/…/project-one")
        self.assertEqual(menu_render.short_folder(os.path.join(self.home, "work"), 16, "…"), "~/work")
        self.assertEqual(menu_render.short_folder(self.home, 16, "…"), "~")
        # Too narrow even for the last part: truncate that part rather than show nothing useful.
        self.assertEqual(menu_render.short_folder("/var/a/b/c/dddddddddddddddddddd", 12, "…"), "ddddddddddd…")
        self.assertEqual(menu_render.short_folder("/var/a/b/c/dd", 12, "…"), "/…/a/b/c/dd")
        self.assertEqual(menu_render.short_folder("", 16, "…"), "")

    def test_the_w_key_toggles_between_a_strip_and_a_panel(self):
        self.assertEqual(menu_render.resize_target(34), menu_render.PANEL_WIDTH)
        self.assertEqual(menu_render.resize_target(20), menu_render.PANEL_WIDTH)
        self.assertEqual(menu_render.resize_target(menu_render.PANEL_WIDTH), menu_render.STRIP_WIDTH)


if __name__ == "__main__":
    unittest.main()
