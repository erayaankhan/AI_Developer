"""Improvements item 18: an author-path deploy (`--requested`) re-checks that the recorded gates / functional / risk
evidence still describes the code on the branch, not only that `state.stage` says the hand-off happened.
git, local_gates and Assembla are mocked; nothing is deployed."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from confirm_support import preview_hash
import dev_ticket
import local_gates
import staging_deploy
from develop_ticket import common
from develop_ticket.guards import handoff_evidence_problem

STATUS = dev_ticket.STATUS
FP = "fp-now"
FILES = ["c2mwf_admin/src/app/a.ts"]
RISK_FILES = ["c2mwf_admin/src/app/a.ts", "AI_DEV_ENV/x.json"]  # a different list, so the two fingerprints can differ


def good_state(**over):
    state = {"ticket": "100", "role": "reviewer", "stage": "8-handoff",
             "gates": {"status": "PASS", "fingerprint": FP, "files": FILES},
             "functional_mode": "run", "functional": {"status": "PASS", "fingerprint": FP},
             "risk_check": {"min_tier": "MEDIUM", "fingerprint": "rfp", "fp_files": RISK_FILES, "fp_auto": False}}
    state.update(over)
    return state


def fake_git(branch="FB_100"):
    return lambda *args, **kw: SimpleNamespace(stdout=branch + "\n" if args[:2] == ("branch", "--show-current") else "")


class Patched(unittest.TestCase):
    """The helper with git and local_gates stubbed: fingerprints come from `self.fp` / `self.risk_fp`."""

    def setUp(self):
        self.fp, self.risk_fp, self.branch, self.dirty = FP, "rfp", "FB_100", []
        for patcher in (
                mock.patch.object(common, "git", side_effect=lambda *a, **k: fake_git(self.branch)(*a, **k)),
                mock.patch.object(common, "uncommitted_tracked", side_effect=lambda *a, **k: self.dirty),
                mock.patch.object(local_gates, "changed_files", return_value=FILES),
                mock.patch.object(local_gates, "risk_files", return_value=RISK_FILES),
                mock.patch.object(local_gates, "diff_fingerprint",
                                  side_effect=lambda files: self.fp if files == FILES else self.risk_fp)):
            patcher.start()
            self.addCleanup(patcher.stop)


class HelperTests(Patched):
    def problem(self, state=None):
        return handoff_evidence_problem("100", state or good_state())

    def test_matching_evidence_is_accepted(self):
        self.assertIsNone(self.problem())

    def test_another_branch_is_refused(self):
        self.branch = "master"
        self.assertEqual(self.problem()[0], "WRONG_BRANCH")

    def test_uncommitted_changes_are_refused(self):
        self.dirty = ["a.ts"]
        self.assertEqual(self.problem()[0], "NOT_COMMITTED")

    def test_a_git_failure_is_reported_not_raised(self):
        with mock.patch.object(common, "git", side_effect=RuntimeError("git branch failed")):
            self.assertEqual(self.problem()[0], "FAILED")
        with mock.patch.object(common, "git", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertEqual(self.problem()[0], "FAILED")

    def test_no_passing_gates_run_is_refused(self):
        for gates in ({}, {"status": "FAIL", "fingerprint": FP}, {"status": "INCOMPLETE", "fingerprint": FP}):
            self.assertEqual(self.problem(good_state(gates=gates))[0], "GATES_REQUIRED", gates)

    def test_no_changes_counts_as_a_passing_gates_run(self):
        state = good_state(gates={"status": "NO_CHANGES", "fingerprint": FP})
        self.assertNotEqual((self.problem(state) or ("",))[0], "GATES_REQUIRED")

    def test_code_pushed_after_the_gates_run_is_stale(self):
        self.fp = "fp-after-a-later-push"
        self.assertEqual(self.problem()[0], "GATES_STALE")

    def test_stale_functional_evidence_is_refused(self):
        state = good_state(functional={"status": "PASS", "fingerprint": "old"})
        self.assertEqual(self.problem(state)[0], "FUNCTIONAL_STALE")

    def test_functional_not_decided_is_refused(self):
        state = good_state()
        state.pop("functional_mode")
        state.pop("functional")
        self.assertIsNotNone(self.problem(state))

    def test_a_risk_check_the_diff_has_outgrown_is_stale(self):
        self.risk_fp = "risk-diff-grew"
        self.assertEqual(self.problem()[0], "RISK_STALE")

    def test_no_recorded_risk_fingerprint_is_not_a_blocker(self):
        state = good_state(risk_check={})
        self.assertIsNone(self.problem(state))


class StatusPathTests(Patched):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state = good_state()
        (self.folder / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["code_review"]}
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}),
                        mock.patch.object(common, "move_db_companion", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def move(self, **kw):
        out = io.StringIO()
        args = SimpleNamespace(ticket="100", to=STATUS["staging"], yes=True, requested=True)
        vars(args).update(kw)
        if args.yes and not hasattr(args, "confirm"):  # as the workflow does: preview first, then apply with its hash
            args.confirm = preview_hash(lambda: dev_ticket.cmd_status(self.api, SimpleNamespace(**{**vars(args), "yes": False})))
        with contextlib.redirect_stdout(out):
            dev_ticket.cmd_status(self.api, args)
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return json.loads(line[len("RESULT: "):])

    def test_current_evidence_lets_the_requested_move_through(self):
        self.assertEqual(self.move()["status"], "UPDATED")

    def test_stale_evidence_stops_the_move(self):
        self.fp = "after-a-later-push"
        res = self.move()
        self.assertEqual(res["status"], "GATES_STALE")
        self.api.update_status.assert_not_called()

    def test_the_preview_also_refuses_stale_evidence(self):
        self.fp = "after-a-later-push"
        self.assertEqual(self.move(yes=False)["status"], "GATES_STALE")

    def test_other_status_moves_do_not_run_the_check(self):
        self.fp = "after-a-later-push"
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["code_review"]}
        res = self.move(requested=False, to=STATUS["done"])  # a LOW-path move: its own guard decides
        self.assertNotEqual(res.get("status"), "GATES_STALE")


class DeployPathTests(unittest.TestCase):
    """staging_deploy._deploy: the check runs after ORIGIN_DIFFERS, only for --requested on a WF ticket."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "DEV_TICKETS" / "100"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps(good_state()), encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["staging"],
                                            "hierarchy_type": staging_deploy.STORY}
        self.evidence = mock.Mock(return_value=("GATES_STALE", "The files changed."))
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(staging_deploy, "PROJECT_ROOT", self.root),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(staging_deploy, "origin_sha", return_value="a" * 40),
                        mock.patch.object(staging_deploy, "local_sha", return_value="a" * 40),
                        mock.patch.object(staging_deploy, "ungated_files", return_value={}),
                        mock.patch.object(staging_deploy, "handoff_evidence_problem", self.evidence)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def deploy(self, **kw):
        out = io.StringIO()
        args = SimpleNamespace(ticket="100", yes=True, requested=True, retry=False, reviewer=False)
        vars(args).update(kw)
        with contextlib.redirect_stdout(out):
            staging_deploy._deploy(self.api, args)
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return json.loads(line[len("RESULT: "):])

    def test_stale_evidence_stops_the_deploy_before_jenkins(self):
        with mock.patch.object(staging_deploy, "queue_position") as queue:
            res = self.deploy()
        self.assertEqual(res["status"], "GATES_STALE")
        self.assertIn("Not triggered", res["message"])
        queue.assert_not_called()

    def test_current_evidence_passes_on_to_the_next_guard(self):
        self.evidence.return_value = None
        with mock.patch.object(staging_deploy, "queue_position", return_value={"status": "NOT_FIRST"}) as queue, \
                mock.patch.object(staging_deploy, "changed_app_folders", return_value={"error": "x"}):
            res = self.deploy()
        queue.assert_called_once()
        self.assertEqual(res["status"], "NOT_FIRST")

    def test_the_evidence_check_runs_after_the_origin_check(self):
        with mock.patch.object(staging_deploy, "local_sha", return_value="b" * 40):
            res = self.deploy()
        self.assertEqual(res["status"], "ORIGIN_DIFFERS")
        self.evidence.assert_not_called()

    def test_the_low_path_does_not_run_it(self):
        with mock.patch.object(staging_deploy, "low_path_problems", return_value=["not low"]):
            res = self.deploy(requested=False)
        self.assertEqual(res["status"], "NOT_LOW_PATH")
        self.evidence.assert_not_called()


if __name__ == "__main__":
    unittest.main()
