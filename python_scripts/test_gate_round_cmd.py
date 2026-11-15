"""`dt gate-round` (improvements item 23): the next automated fix round of the 7a unit-test gate.
Karma logs are written to a temp folder; the start-commit check is mocked, no real test run happens."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dev_ticket
import gate_rounds
import local_gates
import metrics
from develop_ticket import common

KARMA = ("Chrome Headless 153.0.0.0 (Windows 10) {name} FAILED\n"
         "\t{error}\n")


def run(**kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", no_verify=True)
    vars(args).update(kwargs)
    with contextlib.redirect_stdout(out):
        code = dev_ticket.cmd_gate_round(mock.Mock(), args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class GateRoundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state = {"start_head": "abc1234", "gates": {"files": ["c2mwf_admin/src/app/a.component.ts"]}}
        (self.folder / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def log(self, *failures, run_name="20261004-101500"):
        run_dir = self.folder / "gates" / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        text = "".join(KARMA.format(name=n, error=e) for n, e in failures)
        (run_dir / "ng-test-c2mwf_admin.log").write_text(text, encoding="utf-8")
        return run_dir

    def rounds_logged(self):
        return gate_rounds.rounds_used(metrics.read(self.root, dev_ticket.CONFIG["tickets_dir"], "100"))

    def test_without_state_it_asks_for_fetch(self):
        (self.folder / "state.json").unlink()
        code, res = run()
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_without_a_test_log_it_asks_for_a_gates_run(self):
        code, res = run()
        self.assertEqual((code, res["status"]), (1, "NO_TEST_LOG"))

    def test_the_baseline_folder_is_not_a_gates_run(self):
        (self.folder / "gates" / "baseline").mkdir(parents=True)
        (self.folder / "gates" / "baseline" / "ng-test-x.log").write_text("x", encoding="utf-8")
        code, res = run()
        self.assertEqual((code, res["status"]), (1, "NO_TEST_LOG"))

    def test_a_log_without_failures_is_no_failures(self):
        self.log()
        code, res = run()
        self.assertEqual((code, res["status"]), (0, "NO_FAILURES"))
        self.assertEqual(self.rounds_logged(), 0)

    def test_a_round_is_logged_and_a_note_written(self):
        self.log(("Suite a", "Expected 1 to be 2."), ("Suite b", "TypeError: x is not a function"))
        code, res = run()
        self.assertEqual((code, res["status"], res["attempt"], res["cap"]), (0, "ROUND_READY", 1, 3))
        self.assertEqual(self.rounds_logged(), 1)
        self.assertEqual(res["verification"], "skipped (--no-verify)")
        note = Path(res["note"])
        self.assertEqual(note.name, "round-1.md")
        self.assertIn("Suite a", note.read_text(encoding="utf-8"))

    def test_unverified_failures_are_grouped_as_unverified(self):
        self.log(("Suite a", "Expected 1 to be 2."))
        _code, res = run()
        self.assertEqual([f["test"] for f in res["unverified"]], ["Suite a"])
        self.assertEqual((res["auto_fixable"], res["needs_decision"], res["ticket_failures"]), ([], [], []))

    def test_only_the_newest_gates_run_is_read(self):
        self.log(("Old suite", "Expected 1 to be 2."), run_name="20261001-090000")
        self.log(("New suite", "Expected 1 to be 2."), run_name="20261004-101500")
        _code, res = run()
        self.assertEqual([f["test"] for f in res["unverified"]], ["New suite"])

    def test_the_cap_stops_after_three_rounds_and_logs_nothing_more(self):
        self.log(("Suite a", "Expected 1 to be 2."))
        for _ in range(3):
            self.assertEqual(run()[1]["status"], "ROUND_READY")
        code, res = run()
        self.assertEqual((code, res["status"], res["rounds_used"], res["cap"]), (2, "ROUND_CAP", 3, 3))
        self.assertEqual(self.rounds_logged(), 3)

    def test_verification_sorts_failures_by_the_start_commit(self):
        self.log(("Suite setup", "NG0100: ExpressionChangedAfterItHasBeenChecked"),
                 ("Suite old", "Expected 1 to be 2."), ("Suite mine", "Expected 3 to be 4."))
        failing_on_start = [{"test": "Suite setup", "error": "NG0100"}, {"test": "Suite old", "error": "Expected"}]
        outcome = {"ok": True, "failures": failing_on_start, "message": "checked"}
        with mock.patch.object(local_gates, "angular_specs", return_value={"c2mwf_admin": ["a.spec.ts"]}), \
                mock.patch.object(gate_rounds, "start_check", return_value=outcome) as start_check:
            code, res = run(no_verify=False)
        self.assertEqual((code, res["status"]), (0, "ROUND_READY"))
        self.assertEqual([f["test"] for f in res["auto_fixable"]], ["Suite setup"])
        self.assertEqual([f["test"] for f in res["needs_decision"]], ["Suite old"])
        self.assertEqual([f["test"] for f in res["ticket_failures"]], ["Suite mine"])
        self.assertEqual(res["verification"], "c2mwf_admin: checked")
        self.assertEqual(start_check.call_args[0][1:3], ("abc1234", "c2mwf_admin"))

    def test_an_incomplete_start_check_leaves_failures_unverified(self):
        self.log(("Suite a", "Expected 1 to be 2."))
        outcome = {"ok": False, "failures": [], "message": "no node_modules"}
        with mock.patch.object(local_gates, "angular_specs", return_value={"c2mwf_admin": ["a.spec.ts"]}), \
                mock.patch.object(gate_rounds, "start_check", return_value=outcome):
            _code, res = run(no_verify=False)
        self.assertEqual([f["test"] for f in res["unverified"]], ["Suite a"])
        self.assertTrue(res["verification"].startswith("incomplete: "))

    def test_without_a_start_head_verification_is_not_run(self):
        self.state.pop("start_head")
        (self.folder / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        self.log(("Suite a", "Expected 1 to be 2."))
        with mock.patch.object(gate_rounds, "start_check") as start_check:
            _code, res = run(no_verify=False)
        start_check.assert_not_called()
        self.assertEqual(res["verification"], "not run: no start commit recorded")


if __name__ == "__main__":
    unittest.main()
