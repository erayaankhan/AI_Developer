"""Tests for gate_rounds. Run from this folder: python -m unittest test_gate_rounds -v"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gate_rounds as g

ANSI = "\x1b[1A\x1b[2K\x1b[31m"
LOG = (
    "Chrome Headless 153.0.0.0 (Windows 10): Executed 6 of 99 (1 FAILED) (0 secs / 0.6 secs)\n"
    f"{ANSI}Chrome Headless 153.0.0.0 (Windows 10) Suite one saved rows have type FAILED\x1b[39m\n"
    "\tError: NG0100: ExpressionChangedAfterItHasBeenCheckedError: Expression has changed\n"
    "\terror properties: Object({ code: -100 })\n"
    "Chrome Headless 153.0.0.0 (Windows 10): Executed 7 of 99 (1 FAILED) (0 secs / 0.7 secs)\n"
    "Chrome Headless 153.0.0.0 (Windows 10) Suite one saved rows have type FAILED\n"
    "\tError: NG0100: ExpressionChangedAfterItHasBeenCheckedError: Expression has changed\n"
    "Chrome Headless 153.0.0.0 (Windows 10) Suite two compact headers FAILED\n"
    "\tExpected $.length = 3 to equal 2.\n"
    "\tExpected $[1] = 'Description' to equal 'Default Value'.\n"
    "Chrome Headless 153.0.0.0 (Windows 10) Suite three needs a mock FAILED\n"
    "\tTypeError: this.api.getConnectionList is not a function\n"
    "Chrome Headless 153.0.0.0 (Windows 10): Executed 99 of 99 (3 FAILED) (2 secs / 1.7 secs)\n"
)


class ParseTests(unittest.TestCase):
    def test_parses_each_failed_test_once_with_its_first_error_line(self):
        failures = g.parse_karma_failures(LOG)
        self.assertEqual([f["test"] for f in failures],
                         ["Suite one saved rows have type", "Suite two compact headers", "Suite three needs a mock"])
        self.assertTrue(failures[0]["error"].startswith("Error: NG0100"))
        self.assertTrue(failures[1]["error"].startswith("Expected $.length"))

    def test_progress_lines_are_not_failures(self):
        self.assertEqual(g.parse_karma_failures(
            "Chrome Headless 1 (Windows 10): Executed 6 of 99 (1 FAILED) (0 secs / 0.6 secs)\n"), [])

    def test_an_all_green_log_has_no_failures(self):
        self.assertEqual(g.parse_karma_failures("Executed 99 of 99 SUCCESS (2 secs)\n"), [])


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(g.classify("Error: NG0100: ExpressionChangedAfterItHasBeenCheckedError"), "NG0100")
        self.assertEqual(g.classify("Expected true to be false."), "ASSERTION")
        self.assertEqual(g.classify("Expected spy submit to have been called only once"), "ASSERTION")
        self.assertEqual(g.classify("TypeError: this.x.y is not a function"), "RUNTIME")
        self.assertEqual(g.classify("something else"), "OTHER")


class RoundTests(unittest.TestCase):
    def failure(self, category, pre):
        return {"test": f"{category}-{pre}", "error": "", "category": category, "pre_existing": pre}

    def test_counts_only_fix_rounds_of_the_step(self):
        events = [{"event": "fix_round", "step": "7a", "attempt": 1}, {"event": "fix_round", "step": "7e"},
                  {"event": "review", "step": "7a"}, {"event": "fix_round", "step": 7}, {"event": "fix_round", "step": "7a"}]
        self.assertEqual(g.rounds_used(events), 2)

    def test_no_failures(self):
        self.assertEqual(g.plan_round([], 0)["status"], "NO_FAILURES")

    def test_cap_stops_the_loop(self):
        plan = g.plan_round([self.failure("NG0100", True)], g.MAX_ROUNDS)
        self.assertEqual(plan["status"], "ROUND_CAP")

    def test_next_attempt_follows_the_rounds_used(self):
        self.assertEqual(g.plan_round([self.failure("NG0100", True)], 1)["attempt"], 2)

    def test_boundary_a_only_pre_existing_ng0100_is_auto_fixable(self):
        plan = g.plan_round([self.failure("NG0100", True), self.failure("ASSERTION", True),
                             self.failure("RUNTIME", True), self.failure("NG0100", False),
                             self.failure("ASSERTION", None)], 0)
        self.assertEqual([f["test"] for f in plan["auto_fixable"]], ["NG0100-True"])
        self.assertEqual([f["category"] for f in plan["needs_decision"]], ["ASSERTION", "RUNTIME"])
        self.assertEqual([f["test"] for f in plan["ticket_failures"]], ["NG0100-False"])
        self.assertEqual([f["test"] for f in plan["unverified"]], ["ASSERTION-None"])

    def test_mark_pre_existing(self):
        now = [{"test": "a", "error": "Error: NG0100"}, {"test": "b", "error": "Expected x"}]
        marked = g.mark_pre_existing(now, [{"test": "a", "error": ""}], checked=True)
        self.assertEqual([(m["test"], m["pre_existing"], m["category"]) for m in marked],
                         [("a", True, "NG0100"), ("b", False, "ASSERTION")])
        unchecked = g.mark_pre_existing(now, [], checked=False)
        self.assertTrue(all(m["pre_existing"] is None for m in unchecked))


class LinkSafetyTests(unittest.TestCase):
    def test_removing_the_link_never_touches_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "node_modules"
            target.mkdir()
            (target / "keep.txt").write_text("real file")
            link = Path(tmp) / "wt" / "node_modules"
            link.parent.mkdir()
            g.make_junction(link, target)
            self.assertTrue((link / "keep.txt").exists())

            g.remove_link(link)

            self.assertFalse(link.exists())
            self.assertEqual((target / "keep.txt").read_text(), "real file")

    def test_remove_link_ignores_a_plain_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            (plain / "f.txt").write_text("x")
            g.remove_link(plain)
            self.assertTrue((plain / "f.txt").exists())


class StartCheckTests(unittest.TestCase):
    def test_missing_node_modules_is_reported_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = mock.Mock()
            out = g.start_check(Path(tmp), "abc1234", "c2mwf_admin", ["a.spec.ts"], Path(tmp) / "w", 10, run=run)
            self.assertFalse(out["ok"])
            self.assertIn("node_modules is missing", out["message"])
            run.assert_not_called()

    def test_no_specs(self):
        out = g.start_check(Path("."), "abc", "p", [], Path("."), 10, run=mock.Mock())
        self.assertFalse(out["ok"])

    def test_checkout_is_removed_even_when_the_test_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj" / "node_modules").mkdir(parents=True)
            (root / "proj" / "node_modules" / "keep.txt").write_text("real")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd[:3])
                if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
                    (Path(cmd[-2]) / "proj").mkdir(parents=True)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if "ng" in cmd:
                    raise subprocess.TimeoutExpired(cmd, 1)
                if cmd[:3] == ["git", "worktree", "remove"]:
                    import shutil
                    shutil.rmtree(cmd[-1])  # the real git removes the checkout; the link must already be gone
                return subprocess.CompletedProcess(cmd, 0, "", "")

            out = g.start_check(root, "abc1234", "proj", ["x.spec.ts"], root / "work", 1, run=fake_run)

            self.assertFalse(out["ok"])
            self.assertIn("timed out", out["message"])
            self.assertIn(["git", "worktree", "remove"], calls)
            self.assertEqual((root / "proj" / "node_modules" / "keep.txt").read_text(), "real")


if __name__ == "__main__":
    unittest.main()
