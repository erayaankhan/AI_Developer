"""`dt pick`, `dt branch`, `dt status`, `dt handover` and `dt restore` (improvements item 23).
The Assembla API and every git call are mocked: nothing is fetched, pushed, switched or posted anywhere."""
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
from assembla_api import AssemblaError
from develop_ticket import common, state_status

STATUS = dev_ticket.STATUS
ATTRS = dev_ticket.CONFIG["deploy_attributes"]
BASE = dev_ticket.CONFIG["base_branch"]
PREFIX = dev_ticket.CONFIG["branch_prefix"]


def run(command, api, **kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", yes=False)
    vars(args).update(kwargs)
    if args.yes and not hasattr(args, "confirm") and command is dev_ticket.cmd_status:  # as the workflow: preview, then apply with its hash
        args.confirm = preview_hash(lambda: command(api, SimpleNamespace(**{**vars(args), "yes": False})))
    with contextlib.redirect_stdout(out):
        code = command(api, args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class FlowCase(unittest.TestCase):
    """A temp project root, a Mock Assembla api, and no remembered developer/reviewer role."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(common, "_remembered_role", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": 55, "login": "dev"}
        self.api.user_label.return_value = "Dev"

    def make_state(self, **state):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- pick

def ticket(number, status=STATUS["in_process"], assigned=55, milestone=1, **extra):
    return {"number": number, "id": 9000 + number, "summary": f"T{number}", "status": status,
            "assigned_to_id": assigned, "milestone_id": milestone, "updated_at": "2026-01-01", **extra}


class PickTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.api.get_current_milestones.return_value = [{"id": 1, "title": "R1_CURRENT"}]
        self.api.get_my_active_tickets.return_value = []
        self.api.get_milestone_tickets.return_value = []

    def pick(self, **kwargs):
        kwargs.setdefault("ticket", None)
        return run(dev_ticket.cmd_pick, self.api, **kwargs)

    def test_no_current_milestone_stops_everything(self):
        self.api.get_current_milestones.return_value = []
        code, res = self.pick()
        self.assertEqual((code, res["status"]), (0, "NO_CURRENT_MILESTONE"))
        self.api.get_my_active_tickets.assert_not_called()

    def test_no_ticket_in_process_lists_the_queue_and_the_ones_outside_current(self):
        self.api.get_my_active_tickets.return_value = [
            ticket(7, STATUS["queue"]), ticket(8, milestone=2), ticket(9, assigned=99)]
        code, res = self.pick()
        self.assertEqual((code, res["status"]), (0, "NONE"))
        self.assertEqual([r["ticket"] for r in res["dev_queue"]], [7])
        self.assertEqual([r["ticket"] for r in res["in_process_outside_current"]], [8])

    def test_one_ticket_in_process_is_picked(self):
        self.api.get_my_active_tickets.return_value = [ticket(7), ticket(9, assigned=99)]
        code, res = self.pick()
        self.assertEqual((code, res["status"], res["ticket"]), (0, "PICKED", 7))
        self.assertEqual(res["others_in_process"], [])

    def test_two_tickets_in_process_is_a_stop_and_ask(self):
        self.api.get_my_active_tickets.return_value = [ticket(7), ticket(8)]
        code, res = self.pick()
        self.assertEqual((code, res["status"]), (0, "MULTIPLE"))
        self.assertEqual([c["ticket"] for c in res["candidates"]], [7, 8])

    def test_an_empty_my_active_falls_back_to_the_current_milestone_scan(self):
        self.api.get_milestone_tickets.return_value = [ticket(7)]
        _code, res = self.pick()
        self.assertEqual((res["status"], res["ticket"]), ("PICKED", 7))
        self.api.get_milestone_tickets.assert_called_once_with(1)

    def test_a_ticket_in_process_in_another_milestone_is_not_pickable(self):
        self.api.get_my_active_tickets.return_value = [ticket(8, milestone=2)]
        _code, res = self.pick()
        self.assertEqual(res["status"], "NONE")

    def test_an_explicit_ticket_that_does_not_exist(self):
        self.api.get_ticket.side_effect = AssemblaError("404")
        code, res = self.pick(ticket="#100")
        self.assertEqual((code, res["status"], res["ticket"]), (0, "NOT_FOUND", "100"))

    def test_an_explicit_ticket_assigned_to_someone_else(self):
        self.api.get_ticket.return_value = ticket(100, assigned=99)
        _code, res = self.pick(ticket="100")
        self.assertEqual(res["status"], "NOT_ASSIGNED")

    def test_an_explicit_ticket_in_the_wrong_status(self):
        self.api.get_ticket.return_value = ticket(100, STATUS["queue"])
        _code, res = self.pick(ticket="100")
        self.assertEqual(res["status"], "WRONG_STATUS")
        self.assertNotIn("review-and-deploy", res["message"])

    def test_a_code_review_ticket_of_a_reviewer_points_to_review_and_deploy(self):
        self.api.get_ticket.return_value = ticket(100, STATUS["code_review"])
        with mock.patch.object(common, "_remembered_role", return_value="reviewer"):
            _code, res = self.pick(ticket="100")
        self.assertEqual(res["status"], "WRONG_STATUS")
        self.assertIn("/review-and-deploy 100", res["message"])

    def test_an_explicit_ticket_outside_the_current_milestone(self):
        self.api.get_ticket.return_value = ticket(100, milestone=2)
        self.api.get_milestone.return_value = {"title": "Old"}
        _code, res = self.pick(ticket="100")
        self.assertEqual((res["status"], res["milestone"]), ("WRONG_MILESTONE", "Old"))

    def test_an_explicit_good_ticket_is_picked_and_lists_the_others_in_process(self):
        self.api.get_ticket.return_value = ticket(100)
        self.api.get_my_active_tickets.return_value = [ticket(100), ticket(101)]
        _code, res = self.pick(ticket="100")
        self.assertEqual((res["status"], res["ticket"]), ("PICKED", 100))
        self.assertEqual([o["ticket"] for o in res["others_in_process"]], [101])

    def test_pick_never_writes_to_assembla(self):
        self.api.get_my_active_tickets.return_value = [ticket(7)]
        self.pick()
        for name in ("update_status", "add_comment", "update_ticket", "upload_attachment"):
            getattr(self.api, name).assert_not_called()


# ---------------------------------------------------------------------- branch

class FakeGit:
    """Stands in for common.git: answers by the git sub-command and records every call."""

    def __init__(self, current="master", remote_has=False, local_has=False, status="", head="abc1234"):
        self.current, self.remote_has, self.local_has, self.status, self.head = current, remote_has, local_has, status, head
        self.calls = []
        self.fail_on = None

    def __call__(self, *args, check=True, timeout=120, repo=None):
        self.calls.append(args)
        if self.fail_on and args[0] == self.fail_on:
            raise RuntimeError(f"git {args[0]} failed")
        if args[:2] == ("branch", "--show-current"):
            return completed(self.current + "\n")
        if args[0] == "status":
            return completed(self.status)
        if args[0] == "ls-remote":
            return completed("deadbeef\trefs/heads/x\n" if self.remote_has else "")
        if args[0] == "rev-parse" and "--verify" in args:
            return completed("", 0 if self.local_has else 1)
        if args[0] == "rev-parse":
            return completed(self.head + "\n")
        return completed()

    def ran(self, *prefix):
        return any(c[:len(prefix)] == prefix for c in self.calls)


class BranchTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.make_state(stage="3-branch")
        self.epic = {"number": 50, "id": 9050, "summary": "E", "status": "Dev In Process", "hierarchy_type": 3}
        self.api.get_ticket.return_value = {"number": 100, "id": 9100, "hierarchy_type": ATTRS["plan_level"],
                                            "custom_fields": {ATTRS["node_field"]: ATTRS["node_value"]}}
        self.api.get_tags.return_value = [ATTRS["tag"]]
        self.api.get_associations.return_value = [{"id": 1, "ticket1_id": 9100, "ticket2_id": 9050, "relationship": 0}]
        self.api.get_ticket_by_id.return_value = self.epic
        self.git = FakeGit()
        for target, name, value in ((common, "git", self.git),
                                    (common, "master_sync_info", mock.Mock(return_value=(2, 1, ["a.cs"]))),
                                    (common, "start_dirty_files", mock.Mock(return_value=[]))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def branch(self, **kwargs):
        kwargs.setdefault("push", False)
        return run(dev_ticket.cmd_branch, self.api, **kwargs)

    def test_missing_attributes_block_before_any_git_command(self):
        self.api.get_tags.return_value = []
        self.api.get_associations.return_value = []
        code, res = self.branch()
        self.assertEqual((code, res["status"]), (0, "ATTRIBUTES_MISSING"))
        self.assertIn("Tag 'WF' missing", res["problems"])
        self.assertIn("No Epic parent", res["problems"])
        self.assertEqual(res["branch"], self.branch_name_value)
        self.assertEqual(self.git.calls, [])

    @property
    def branch_name_value(self):
        return f"{PREFIX}100"

    def test_wrong_plan_level_and_node_are_reported(self):
        self.api.get_ticket.return_value = {"number": 100, "id": 9100, "hierarchy_type": 99, "custom_fields": {}}
        _code, res = self.branch()
        self.assertEqual(res["status"], "ATTRIBUTES_MISSING")
        self.assertTrue(any(p.startswith("Plan level") for p in res["problems"]))
        self.assertTrue(any(p.startswith(ATTRS["node_field"]) for p in res["problems"]))

    def test_an_inactive_epic_counts_as_no_epic(self):
        self.epic["status"] = "Closed"
        _code, res = self.branch()
        self.assertEqual(res["status"], "ATTRIBUTES_MISSING")
        self.assertTrue(any(p.startswith("Epic parent is inactive") for p in res["problems"]))

    def test_a_dirty_tree_on_a_fresh_start_is_refused_and_nothing_is_switched(self):
        with mock.patch.object(common, "start_dirty_files", return_value=[" M a.cs", "?? b.txt", "?? c.txt"]):
            code, res = self.branch()
        self.assertEqual((code, res["status"]), (0, "DIRTY"))
        self.assertEqual((res["tracked"], res["untracked"], res["total"]), (1, 2, 3))
        self.assertFalse(self.git.ran("switch"))
        self.assertNotIn("start_head", self.state())

    def test_tracked_changes_block_a_switch_on_a_resumed_ticket(self):
        self.make_state(stage="6-build", start_head="abc")
        self.git.status = " M src/a.cs\n"
        _code, res = self.branch()
        self.assertEqual(res["status"], "DIRTY")
        self.assertEqual(res["current_branch"], "master")
        self.assertFalse(self.git.ran("switch"))

    def test_local_only_files_do_not_count_as_dirty(self):
        self.make_state(stage="6-build", start_head="abc")
        self.git.status = " M c2mwf_admin/src/environments/environment.ts\n"
        _code, res = self.branch()
        self.assertEqual(res["status"], "CREATED_LOCAL")

    def test_a_new_branch_is_created_from_origin_base_and_not_pushed_without_push(self):
        code, res = self.branch()
        self.assertEqual((code, res["status"], res["pushed"]), (0, "CREATED_LOCAL", False))
        self.assertTrue(self.git.ran("switch", "--no-track", "-c", self.branch_name_value, f"origin/{BASE}"))
        self.assertFalse(self.git.ran("push"))
        self.assertEqual((res["ahead"], res["behind"], res["merge_conflicts"]), (2, 1, ["a.cs"]))
        self.assertTrue(res["baseline_required"])
        state = self.state()
        self.assertEqual((state["start_head"], state["branch_origin"]), ("abc1234", "created"))

    def test_push_creates_the_branch_on_origin(self):
        _code, res = self.branch(push=True)
        self.assertEqual((res["status"], res["pushed"]), ("CREATED_PUSHED", True))
        self.assertTrue(self.git.ran("push", "-u", "origin", self.branch_name_value))

    def test_an_existing_local_branch_is_checked_out(self):
        self.git.local_has, self.git.remote_has = True, True
        _code, res = self.branch()
        self.assertEqual((res["status"], res["pushed"]), ("CHECKED_OUT", True))
        self.assertTrue(self.git.ran("switch", self.branch_name_value))
        self.assertEqual(self.state()["branch_origin"], "existing")
        self.assertFalse(res["baseline_required"])

    def test_a_remote_only_branch_is_fetched_and_tracked(self):
        self.git.remote_has = True
        _code, res = self.branch(push=True)
        self.assertEqual(res["status"], "CHECKED_OUT_REMOTE")
        self.assertTrue(self.git.ran("switch", "--track", f"origin/{self.branch_name_value}"))
        self.assertFalse(self.git.ran("push"))

    def test_already_on_the_branch_switches_nothing(self):
        self.git.current, self.git.local_has = self.branch_name_value, True
        _code, res = self.branch()
        self.assertEqual(res["status"], "ALREADY_ON_BRANCH")
        self.assertFalse(self.git.ran("switch"))

    def test_a_paused_ticket_is_a_resume_and_keeps_its_state(self):
        self.make_state(stage="paused", paused_from="6-build")
        self.git.local_has, self.git.remote_has = True, True
        _code, res = self.branch()
        self.assertEqual(res["status"], "CHECKED_OUT")
        self.assertNotIn("start_head", self.state())
        self.assertFalse(res["baseline_required"])

    def test_a_ticket_with_a_start_head_is_not_a_fresh_start(self):
        self.make_state(stage="3-branch", start_head="old")
        with mock.patch.object(common, "start_dirty_files", return_value=[" M a.cs"]) as dirty:
            _code, res = self.branch()
        dirty.assert_not_called()
        self.assertEqual(self.state()["start_head"], "old")
        self.assertFalse(res["baseline_required"])

    def test_without_a_state_file_the_branch_still_works_and_writes_no_state(self):
        (self.folder / "state.json").unlink()
        _code, res = self.branch()
        self.assertEqual(res["status"], "CREATED_LOCAL")
        self.assertFalse((self.folder / "state.json").exists())

    def test_a_git_failure_is_reported_as_failed_with_exit_1(self):
        self.git.fail_on = "switch"
        code, res = self.branch()
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertIn("switch", res["message"])

    def test_a_git_timeout_is_reported_as_failed(self):
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("git", 1)
        with mock.patch.object(common, "git", boom):
            code, res = self.branch()
        self.assertEqual((code, res["status"]), (1, "FAILED"))

    def test_branch_never_writes_to_assembla(self):
        self.branch(push=True)
        for name in ("update_status", "add_comment", "update_ticket", "add_parent"):
            getattr(self.api, name).assert_not_called()


# ---------------------------------------------------------------------- status

class StatusTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.t = ticket(100, STATUS["in_process"])
        self.api.get_ticket.side_effect = lambda n: self.t if str(n) == "100" else self.other.get(str(n), {})
        self.other = {}
        self.pushed = mock.Mock(return_value={"status": "PUSHED", "branch": "FB_100", "head": "abc"})
        patcher = mock.patch.object(common, "push_state", self.pushed)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.get_current_milestones.return_value = [{"id": 1}]
        self.api.get_my_active_tickets.return_value = []
        self.api.get_milestone_tickets.return_value = []
        self.make_state(stage="8-handoff", role="developer", risk="MEDIUM", path="STANDARD")

    def status(self, to, **kwargs):
        kwargs.setdefault("requested", False)
        return run(dev_ticket.cmd_status, self.api, to=to, **kwargs)

    def test_a_move_not_in_the_table_is_refused_and_lists_the_allowed_ones(self):
        code, res = self.status(STATUS["done"], yes=True)
        self.assertEqual((code, res["status"]), (0, "NOT_ALLOWED"))
        self.assertIn(f"{STATUS['in_process']} -> {STATUS['code_review']}", res["allowed"])
        self.api.update_status.assert_not_called()

    def test_a_low_path_move_on_a_medium_ticket_is_refused_with_reasons(self):
        self.t["status"] = STATUS["code_review"]
        _code, res = self.status(STATUS["done"], yes=True)
        self.assertEqual(res["status"], "NOT_ALLOWED")
        self.assertTrue(any("not LOW" in r for r in res["reasons"]))
        self.api.update_status.assert_not_called()

    def test_a_low_path_move_without_a_risk_check_is_refused(self):
        self.make_state(stage="8-handoff", role="reviewer", risk="LOW", path="LOW")
        self.t["status"] = STATUS["code_review"]
        _code, res = self.status(STATUS["done"], yes=True)
        self.assertEqual(res["status"], "NOT_ALLOWED")
        self.assertTrue(any("risk_check.min_tier" in r for r in res["reasons"]))

    def test_a_low_path_move_without_a_work_folder_is_refused(self):
        (self.folder / "state.json").unlink()
        self.t["status"] = STATUS["code_review"]
        _code, res = self.status(STATUS["done"], yes=True)
        self.assertEqual(res["reasons"], ["no state.json for this ticket"])

    def test_a_requested_move_needs_a_finished_hand_off(self):
        self.t["status"] = STATUS["code_review"]
        self.make_state(stage="6-build", role="reviewer")
        _code, res = run(dev_ticket.cmd_status, self.api, to=STATUS["staging"], requested=True, yes=True)
        self.assertEqual((res["status"], res["stage"]), ("NOT_HANDED_OFF", "6-build"))
        self.api.update_status.assert_not_called()

    @mock.patch("develop_ticket.state_status.handoff_evidence_problem", return_value=None)  # covered in test_handoff_evidence.py
    def test_a_requested_move_after_hand_off_previews_then_applies(self, _evidence):
        self.t["status"] = STATUS["code_review"]
        self.make_state(stage="8-handoff", role="reviewer")
        _code, res = run(dev_ticket.cmd_status, self.api, to=STATUS["staging"], requested=True)
        self.assertEqual(res["status"], "PREVIEW")
        self.api.update_status.assert_not_called()
        _code, res = run(dev_ticket.cmd_status, self.api, to=STATUS["staging"], requested=True, yes=True)
        self.assertEqual((res["status"], res["previous"], res["current"]),
                         ("UPDATED", STATUS["code_review"], STATUS["staging"]))
        self.api.update_status.assert_called_once_with("100", STATUS["staging"])

    def test_a_developer_may_not_move_a_ticket_to_staging(self):
        self.t["status"] = STATUS["code_review"]
        _code, res = run(dev_ticket.cmd_status, self.api, to=STATUS["staging"], requested=True, yes=True)
        self.assertEqual(res["status"], "NOT_A_REVIEWER")
        self.api.update_status.assert_not_called()

    def test_the_requested_flag_does_not_open_other_moves(self):
        _code, res = run(dev_ticket.cmd_status, self.api, to=STATUS["staging"], requested=True, yes=True)
        self.assertEqual(res["status"], "NOT_ALLOWED")

    def test_no_work_folder_means_no_status_move(self):
        (self.folder / "state.json").unlink()
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["status"], "NO_STATE")
        self.api.update_status.assert_not_called()

    def test_a_review_folder_is_refused(self):
        self.make_state(kind="review", stage="done")
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["status"], "REVIEW_FOLDER")
        self.api.update_status.assert_not_called()

    def test_code_review_needs_everything_pushed(self):
        self.pushed.return_value = {"status": "NOT_COMMITTED", "branch": "FB_100"}
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["status"], "NOT_PUSHED")
        self.assertEqual(res["repos"][0]["status"], "NOT_COMMITTED")
        self.api.update_status.assert_not_called()

    def test_a_db_branch_that_is_not_pushed_blocks_code_review(self):
        self.make_state(stage="8-handoff", role="developer", db={"ticket": 200, "files": ["a.sql"]})
        self.pushed.side_effect = [{"status": "PUSHED"}, {"status": "NOT_PUSHED"}]
        _code, res = self.status(STATUS["code_review"], yes=True, confirm=None)  # refused before the confirm check; no preview run
        self.assertEqual(res["status"], "NOT_PUSHED")
        self.assertEqual(self.pushed.call_args_list[1], mock.call("200", common.DB_REPO))

    def test_code_review_previews_without_yes_and_changes_nothing(self):
        code, res = self.status(STATUS["code_review"])
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.api.update_status.assert_not_called()
        self.assertFalse((self.folder / "metrics.jsonl").exists())

    def test_code_review_with_yes_updates_the_status_and_records_a_metric(self):
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual((res["status"], res["previous"], res["current"]),
                         ("UPDATED", STATUS["in_process"], STATUS["code_review"]))
        self.api.update_status.assert_called_once_with("100", STATUS["code_review"])
        event = json.loads((self.folder / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((event["event"], event["previous"], event["current"]),
                         ("status", STATUS["in_process"], STATUS["code_review"]))

    def test_the_db_ticket_follows_the_main_ticket(self):
        self.make_state(stage="8-handoff", role="developer", db={"ticket": 200, "files": ["a.sql"]})
        self.other["200"] = {"number": 200, "status": STATUS["in_process"]}
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["db_companion"], {"db_ticket": 200, "moved": True, "to": STATUS["code_review"]})
        self.assertEqual(self.api.update_status.call_args_list,
                         [mock.call("100", STATUS["code_review"]), mock.call(200, STATUS["code_review"])])

    def test_a_db_ticket_that_is_elsewhere_is_reported_not_moved(self):
        self.make_state(stage="8-handoff", role="developer", db={"ticket": 200, "files": ["a.sql"]})
        self.other["200"] = {"number": 200, "status": STATUS["queue"]}
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertFalse(res["db_companion"]["moved"])
        self.api.update_status.assert_called_once_with("100", STATUS["code_review"])

    def test_a_defect_without_rca_and_clca_cannot_go_to_code_review(self):
        self.make_state(stage="8-handoff", role="developer", type="DEFECT")
        with mock.patch.object(state_status, "rca_clca_missing", return_value=["RCA"]):
            _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual((res["status"], res["missing"]), ("RCA_CLCA_MISSING", ["RCA"]))
        self.api.update_status.assert_not_called()

    def test_a_defect_without_a_recorded_sweep_cannot_go_to_code_review(self):
        self.make_state(stage="8-handoff", role="developer", type="DEFECT")
        with mock.patch.object(state_status, "rca_clca_missing", return_value=[]), \
                mock.patch.object(state_status, "sweep_check", return_value=["no sweep"]):
            _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual((res["status"], res["problems"]), ("SWEEP_MISSING", ["no sweep"]))
        self.api.update_status.assert_not_called()

    def test_a_complete_defect_goes_to_code_review(self):
        self.make_state(stage="8-handoff", role="developer", type="DEFECT")
        with mock.patch.object(state_status, "rca_clca_missing", return_value=[]), \
                mock.patch.object(state_status, "sweep_check", return_value=[]):
            _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["status"], "UPDATED")

    def test_in_process_needs_the_ticket_assigned_to_me(self):
        self.t.update(status=STATUS["queue"], assigned_to_id=99)
        _code, res = self.status(STATUS["in_process"], yes=True)
        self.assertEqual(res["status"], "NOT_ASSIGNED")
        self.api.update_status.assert_not_called()

    def test_only_one_ticket_may_be_in_process(self):
        self.t["status"] = STATUS["queue"]
        self.api.get_current_milestones.return_value = [{"id": 1}]
        self.api.get_my_active_tickets.return_value = [ticket(101)]
        _code, res = self.status(STATUS["in_process"], yes=True)
        self.assertEqual((res["status"], res["others"]), ("OTHER_IN_PROCESS", ["101"]))
        self.api.update_status.assert_not_called()

    def test_the_ticket_itself_in_the_in_process_list_is_not_another_ticket(self):
        self.t["status"] = STATUS["queue"]
        self.api.get_current_milestones.return_value = [{"id": 1}]
        self.api.get_my_active_tickets.return_value = [ticket(100)]
        _code, res = self.status(STATUS["in_process"])
        self.assertEqual(res["status"], "PREVIEW")

    def test_back_to_in_process_applies_without_a_push_check(self):
        self.t["status"] = STATUS["queue"]
        self.api.get_current_milestones.return_value = [{"id": 1}]
        self.api.get_my_active_tickets.return_value = []
        _code, res = self.status(STATUS["in_process"], yes=True)
        self.assertEqual(res["status"], "UPDATED")
        self.api.update_status.assert_called_once_with("100", STATUS["in_process"])
        self.pushed.assert_not_called()

    def test_a_pause_move_to_the_queue_needs_no_push(self):
        _code, res = self.status(STATUS["queue"], yes=True)
        self.assertEqual(res["status"], "UPDATED")
        self.pushed.assert_not_called()


class StatusGuardTests(FlowCase):
    """status_guard on its own: (status, fields) or None."""

    def test_returns_none_when_every_rule_is_met(self):
        self.make_state(stage="8-handoff", role="developer")
        with mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}):
            self.assertIsNone(state_status.status_guard(self.api, "100", ticket(100), STATUS["code_review"]))

    def test_no_state_gives_no_state(self):
        refused = state_status.status_guard(self.api, "100", ticket(100), STATUS["code_review"])
        self.assertEqual(refused[0], "NO_STATE")

    def test_a_review_folder_gives_review_folder(self):
        self.make_state(kind="review")
        self.assertEqual(state_status.status_guard(self.api, "100", ticket(100), STATUS["code_review"])[0],
                         "REVIEW_FOLDER")

    def test_a_developer_is_refused_dev_done_and_a_reviewer_is_not(self):
        self.make_state(role="developer")
        self.assertEqual(state_status.status_guard(self.api, "100", ticket(100), STATUS["done"])[0], "NOT_A_REVIEWER")
        self.make_state(role="reviewer")
        with mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}):
            self.assertIsNone(state_status.status_guard(self.api, "100", ticket(100), STATUS["done"]))

    def test_a_ticket_not_pushed_reports_the_repos(self):
        self.make_state(role="reviewer")
        with mock.patch.object(common, "push_state", return_value={"status": "WRONG_BRANCH"}):
            refused = state_status.status_guard(self.api, "100", ticket(100), STATUS["done"])
        self.assertEqual((refused[0], refused[1]["repos"]), ("NOT_PUSHED", [{"status": "WRONG_BRANCH"}]))


# ---------------------------------------------------------------------- handover / restore

class HandoverTests(FlowCase):
    def setUp(self):
        super().setUp()
        self.make_state(stage="6-build", risk="LOW", path="LOW")
        (self.folder / "BRIEF.md").write_text("the brief\n", encoding="utf-8")
        (self.folder / "PLAN.md").write_text("the plan\n", encoding="utf-8")
        self.notes = self.folder / "notes.md"
        self.notes.write_text("Left off at the controller.\n", encoding="utf-8")
        self.api.get_ticket.side_effect = lambda n: (
            {"id": 9100, "summary": "Fix it", "status": self.status} if str(n) == "100"
            else self.db_ticket)
        self.status = STATUS["in_process"]
        self.db_ticket = {"id": 9200, "status": STATUS["in_process"]}
        self.pushed = mock.Mock(return_value={"status": "PUSHED", "branch": "FB_100", "head": "abc123"})
        patcher = mock.patch.object(common, "push_state", self.pushed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def handover(self, **kwargs):
        kwargs.setdefault("notes", str(self.notes))
        return run(dev_ticket.cmd_handover, self.api, **kwargs)

    def uploads(self):
        return sorted(p.name for p in self.folder.glob("HANDOVER-100-*.md"))

    def test_no_state_is_refused(self):
        (self.folder / "state.json").unlink()
        code, res = self.handover(yes=True)
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_notes_outside_the_ticket_folder_are_never_attached(self):
        outside = self.root / "credentials.env"
        outside.write_text("KEY=secret", encoding="utf-8")
        code, res = self.handover(notes=str(outside), yes=True)
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))
        self.api.upload_attachment.assert_not_called()

    def test_missing_notes_are_refused(self):
        code, res = self.handover(notes=str(self.folder / "none.md"), yes=True)
        self.assertEqual((code, res["status"]), (1, "NO_NOTES"))

    def test_unpushed_work_stops_the_hand_over_before_anything_is_written(self):
        self.pushed.return_value = {"status": "NOT_PUSHED", "branch": "FB_100"}
        code, res = self.handover(yes=True)
        self.assertEqual((code, res["status"]), (0, "NOT_PUSHED"))
        self.assertEqual(self.uploads(), [])
        self.assertFalse((self.folder / "HANDOVER.md").exists())
        self.api.upload_attachment.assert_not_called()

    def test_an_unpushed_db_branch_also_stops_it(self):
        self.make_state(stage="6-build", db={"ticket": 200, "files": ["a.sql"]})
        self.pushed.side_effect = [{"status": "PUSHED", "head": "abc"}, {"status": "NOT_COMMITTED", "head": "x"}]
        _code, res = self.handover(yes=True)
        self.assertEqual(res["status"], "NOT_PUSHED")
        self.assertEqual(len(res["repos"]), 2)
        self.api.upload_attachment.assert_not_called()

    def test_without_yes_it_builds_the_file_and_previews_only(self):
        code, res = self.handover()
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertTrue(any(p.startswith("attach HANDOVER-100-") for p in res["planned"]))
        self.assertIn(f"status {STATUS['in_process']} -> {STATUS['queue']}", res["planned"])
        text = (self.folder / "HANDOVER.md").read_text(encoding="utf-8")
        self.assertIn("Left off at the controller.", text)
        self.assertIn("<!-- BEGIN BRIEF.md -->\nthe brief\n<!-- END BRIEF.md -->", text)
        self.assertIn("<!-- BEGIN state.json -->", text)
        for name in ("upload_attachment", "add_comment", "update_status"):
            getattr(self.api, name).assert_not_called()
        self.assertEqual(self.state()["stage"], "6-build")

    def test_yes_attaches_comments_moves_to_the_queue_and_marks_the_state_paused(self):
        code, res = self.handover(yes=True)
        self.assertEqual((code, res["status"], res["paused_from"], res["status_moved"]), (0, "PAUSED", "6-build", True))
        name = self.uploads()[0]
        self.assertEqual(res["attached"], name)
        self.api.upload_attachment.assert_called_once_with(9100, self.folder / name)
        comment = self.api.add_comment.call_args[0]
        self.assertEqual(comment[0], "100")
        self.assertTrue(comment[1].startswith("[dev-ticket pause]"))
        self.assertIn(name, comment[1])
        self.api.update_status.assert_called_once_with("100", STATUS["queue"])
        state = self.state()
        self.assertEqual((state["stage"], state["paused_from"], state["paused_by"]), ("paused", "6-build", "dev"))
        self.assertEqual(state["history"][-1]["result"], "HANDOVER_POSTED")

    def test_the_uploaded_file_holds_the_state_as_it_was_before_pausing(self):
        self.handover(yes=True)
        text = (self.folder / self.uploads()[0]).read_text(encoding="utf-8")
        self.assertIn('"stage": "6-build"', text)
        self.assertIn("Branch: FB_100 @ abc123 (pushed)", text)
        self.assertIn("DB: none", text)

    def test_a_ticket_not_in_process_is_not_moved(self):
        self.status = STATUS["queue"]
        _code, res = self.handover(yes=True)
        self.assertEqual((res["status"], res["status_moved"]), ("PAUSED", False))
        self.api.update_status.assert_not_called()
        self.api.upload_attachment.assert_called_once()

    def test_the_db_ticket_follows_to_the_queue_and_its_branch_is_in_the_header(self):
        self.make_state(stage="6-build", db={"ticket": 200, "files": ["a.sql", "b.sql"]})
        self.pushed.side_effect = [{"status": "PUSHED", "head": "wf1"}, {"status": "PUSHED", "head": "db1"}]
        _code, res = self.handover(yes=True)
        self.assertEqual(res["db_companion"], {"db_ticket": 200, "moved": True, "to": STATUS["queue"]})
        self.assertEqual(self.api.update_status.call_args_list,
                         [mock.call("100", STATUS["queue"]), mock.call(200, STATUS["queue"])])
        self.assertIn("ticket #200, branch FB_200 @ db1, files: a.sql, b.sql",
                      (self.folder / "HANDOVER.md").read_text(encoding="utf-8"))

    def test_a_missing_brief_is_embedded_empty_not_an_error(self):
        (self.folder / "BRIEF.md").unlink()
        _code, res = self.handover(yes=True)
        self.assertEqual(res["status"], "PAUSED")
        self.assertIn("<!-- BEGIN BRIEF.md -->\n\n<!-- END BRIEF.md -->",
                      (self.folder / "HANDOVER.md").read_text(encoding="utf-8"))


class RestoreTests(FlowCase):
    NAME = "HANDOVER-100-20260101-1200.md"

    def handover_text(self, state='{"stage": "paused", "paused_from": "6-build", "paused_by": "dev"}',
                      brief="the brief"):
        text = f"# Hand-over\n\n<!-- BEGIN BRIEF.md -->\n{brief}\n<!-- END BRIEF.md -->\n\n"
        text += f"<!-- BEGIN PLAN.md -->\n\n<!-- END PLAN.md -->\n\n"
        if state is not None:
            text += f"<!-- BEGIN state.json -->\n{state}\n<!-- END state.json -->\n"
        return text

    def attachments(self, *items):
        self.api.get_attachments.return_value = list(items)
        self.api.download_attachment.side_effect = lambda a: a["body"].encode("utf-8")

    def att(self, name=None, at="2026-01-01T12:00:00Z", **kw):
        return {"name": name or self.NAME, "created_at": at, "body": kw.get("body", self.handover_text())}

    def restore(self, **kwargs):
        kwargs.setdefault("force", False)
        return run(dev_ticket.cmd_restore, self.api, **kwargs)

    def test_a_ticket_without_a_hand_over_attachment_is_reported(self):
        self.attachments(self.att(name="screenshot.png"), self.att(name="HANDOVER-101-20260101-1200.md"))
        code, res = self.restore()
        self.assertEqual((code, res["status"]), (0, "NO_HANDOVER"))
        self.assertFalse(self.folder.exists())

    def test_an_existing_work_folder_is_not_overwritten_without_force(self):
        self.make_state(stage="6-build")
        self.attachments(self.att())
        _code, res = self.restore()
        self.assertEqual((res["status"], res["local_stage"], res["attachment"]), ("LOCAL_EXISTS", "6-build", self.NAME))
        self.assertEqual(self.state()["stage"], "6-build")
        self.api.download_attachment.assert_not_called()

    def test_restore_rebuilds_the_work_folder_from_the_latest_attachment(self):
        self.attachments(self.att(name="HANDOVER-100-20250101-1000.md", at="2025-01-01", body=self.handover_text(brief="old")),
                         self.att())
        code, res = self.restore()
        self.assertEqual((code, res["status"], res["attachment"]), (0, "RESTORED", self.NAME))
        self.assertEqual(res["restored"], ["BRIEF.md", "state.json"])  # an empty PLAN block is not restored
        self.assertEqual((res["resume_stage"], res["paused_by"]), ("6-build", "dev"))
        self.assertEqual((self.folder / "BRIEF.md").read_text(encoding="utf-8"), "the brief\n")
        self.assertFalse((self.folder / "PLAN.md").exists())
        self.assertTrue((self.folder / "HANDOVER.md").is_file())
        state = self.state()
        self.assertEqual(state["history"][-1]["stage"], "restored")
        self.assertEqual(state["history"][-1]["result"], self.NAME)

    def test_the_latest_is_chosen_by_created_at_not_by_name_or_order(self):
        self.attachments(self.att(at="2026-02-01", body=self.handover_text(brief="newest")),
                         self.att(name="HANDOVER-100-20260301-0900.md", at="2026-01-01",
                                  body=self.handover_text(brief="older")))
        _code, res = self.restore()
        self.assertEqual(res["attachment"], self.NAME)
        self.assertEqual((self.folder / "BRIEF.md").read_text(encoding="utf-8"), "newest\n")

    def test_force_overwrites_the_local_folder(self):
        self.make_state(stage="3-branch")
        self.attachments(self.att())
        _code, res = self.restore(force=True)
        self.assertEqual(res["status"], "RESTORED")
        self.assertEqual(self.state()["paused_from"], "6-build")

    def test_a_hand_over_without_state_json_is_incomplete_with_exit_1(self):
        self.attachments(self.att(body=self.handover_text(state=None)))
        code, res = self.restore()
        self.assertEqual((code, res["status"]), (1, "INCOMPLETE"))
        self.assertEqual(res["restored"], ["BRIEF.md"])

    def test_resume_stage_falls_back_to_the_stage_when_not_paused_from(self):
        self.attachments(self.att(body=self.handover_text(state='{"stage": "5-plan"}')))
        _code, res = self.restore()
        self.assertEqual(res["resume_stage"], "5-plan")

    def test_restore_never_writes_to_assembla(self):
        self.attachments(self.att())
        self.restore()
        for name in ("update_status", "add_comment", "upload_attachment"):
            getattr(self.api, name).assert_not_called()

    def test_a_handover_round_trips_through_restore(self):
        self.make_state(stage="6-build", risk="LOW", path="LOW")
        (self.folder / "BRIEF.md").write_text("round trip brief\n", encoding="utf-8")
        notes = self.folder / "notes.md"
        notes.write_text("notes", encoding="utf-8")
        self.api.get_ticket.return_value = {"id": 9100, "summary": "S", "status": STATUS["queue"]}
        with mock.patch.object(common, "push_state", return_value={"status": "PUSHED", "head": "abc"}):
            _c, res = run(dev_ticket.cmd_handover, self.api, notes=str(notes), yes=True)
        uploaded = self.folder / res["attached"]
        body = uploaded.read_text(encoding="utf-8")
        for p in self.folder.iterdir():
            p.unlink()
        self.api.get_attachments.return_value = [{"name": res["attached"], "created_at": "x"}]
        self.api.download_attachment.side_effect = lambda a: body.encode("utf-8")
        code, res = self.restore()
        self.assertEqual((code, res["status"], res["resume_stage"]), (0, "RESTORED", "6-build"))
        self.assertEqual((self.folder / "BRIEF.md").read_text(encoding="utf-8"), "round trip brief\n")
        self.assertEqual(self.state()["risk"], "LOW")


if __name__ == "__main__":
    unittest.main()
