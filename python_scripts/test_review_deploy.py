"""Phase 4 of the reviewer review (REVIEW_TICKET_PHASE4_PLAN.md): the reviewer deploy mode. Assembla, git and the Jenkins
subprocess are mocked; nothing is deployed."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
from develop_ticket import common, review
import staging_deploy


def capture(call):
    """Run a handler that prints a RESULT line; -> (exit code, parsed RESULT)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = call()
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class ConfigTests(unittest.TestCase):
    def test_reviewer_status_moves_cover_verdict_deploy_request_and_withdraw(self):
        moves = dev_ticket.CONFIG["reviewer_status_moves"]
        for move in (["Code Review", "Dev Done"], ["Code Review", "Dev Queue"], ["Dev Done", "DevOps Staging"],
                     ["Dev Done", "Code Review"]):
            self.assertIn(move, moves)
        known = set(dev_ticket.STATUS.values())
        self.assertTrue(all(a in known and b in known for a, b in moves))


class ReviewerFolderIsClosedToTheAuthorPathTests(unittest.TestCase):
    """A review folder has stage `done`, which the author path accepts: it must refuse the folder instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        self.write_state({"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done",
                          "review": {"stage": "R6-deploy", "approved_head": "a" * 40}})
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1", "number": 100}
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(staging_deploy, "PROJECT_ROOT", self.root),  # owner_of scans this one
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(common, "push_state", return_value={"status": "PUSHED"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def test_status_guard_refuses_every_target_for_a_reviewer_folder(self):
        for target in ("Dev Done", "DevOps Staging", "Code Review", "Dev In Process", "Dev Queue"):
            status, fields = dev_ticket.status_guard(self.api, "100", {}, target)
            self.assertEqual(status, "REVIEW_FOLDER", target)
            self.assertIn("review-and-deploy", fields["message"])

    def test_the_old_requested_deploy_request_is_refused_for_a_reviewer_folder(self):
        args = mock.Mock(ticket="100", to="DevOps Staging", yes=True, requested=True)
        code, res = capture(lambda: dev_ticket.cmd_status(self.api, args))
        self.assertEqual(res["status"], "REVIEW_FOLDER")
        self.api.update_status.assert_not_called()

    def test_an_authors_status_moves_are_unchanged(self):
        for role in ("developer", "reviewer"):
            self.write_state({"ticket": "100", "role": role, "stage": "done"})
            with mock.patch.object(common, "_remembered_role", return_value=role):
                guard = dev_ticket.status_guard(self.api, "100", {}, "Code Review")
            self.assertIsNone(guard, role)
        self.write_state({"ticket": "100", "role": "reviewer", "stage": "done"})
        self.assertIsNone(dev_ticket.status_guard(self.api, "100", {}, "DevOps Staging"))

    def deploy(self, **over):
        args = mock.Mock(ticket="100", yes=True, requested=True, retry=False, reviewer=False)
        for key, value in over.items():
            setattr(args, key, value)
        return capture(lambda: staging_deploy._deploy(self.api, args))

    def test_staging_deploy_author_modes_refuse_a_reviewer_folder(self):
        for requested in (True, False):  # --requested and the LOW path
            _code, res = self.deploy(requested=requested)
            self.assertEqual(res["status"], "USE_REVIEWER_MODE", requested)

    def test_the_db_tickets_owner_decides_too(self):
        self.api.get_ticket.return_value = {"status": "DevOps Staging", "number": 200,
                                            "custom_fields": {"Node": dev_ticket.CONFIG["db"]["node_value"]}}
        self.write_state({"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done", "db": {"ticket": 200, "files": ["x.sql"]}})
        _code, res = capture(lambda: staging_deploy._deploy(self.api, mock.Mock(
            ticket="200", yes=True, requested=True, retry=False, reviewer=False)))
        self.assertEqual(res["status"], "USE_REVIEWER_MODE")

    def test_an_authors_deploy_gets_past_the_new_guard(self):
        self.write_state({"ticket": "100", "role": "reviewer", "stage": "8-handoff"})
        _code, res = self.deploy()  # the ticket is in Code Review: it stops at the next, unchanged guard
        self.assertEqual(res["status"], "WRONG_STATUS")

    def test_the_reviewer_flag_is_not_stopped_by_this_guard(self):
        _code, res = self.deploy(reviewer=True)
        self.assertNotEqual(res["status"], "USE_REVIEWER_MODE")


A, B = "a" * 40, "b" * 40


def approved_state(**over):
    state = {"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done", "branch": "FB_100",
             "review": {"stage": "R6-deploy", "author": "developer1", "head": A, "approved_head": A,
                        "verdict": {"verdict": "APPROVE", "round": 1, "head": A}}}
    state.update(over)
    return state


class ReviewDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        self.write_state(approved_state())
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        self.tips = {"FB_100": ("OK", A), "FB_200": ("OK", B)}
        self.companion = mock.Mock(return_value=None)
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1"]}),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(review, "fetch_origin_branch", side_effect=lambda b, repo=None: self.tips[b]),
                        mock.patch.object(common, "move_db_companion", self.companion)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, yes=False, ticket="100"):
        return capture(lambda: dev_ticket.cmd_review_deploy(self.api, mock.Mock(ticket=ticket, yes=yes)))[1]

    def test_preview_changes_nothing_and_names_the_next_steps(self):
        res = self.go()
        self.assertEqual((res["status"], res["approved_head"]), ("PREVIEW", A[:12]))
        self.assertTrue(any("staging_deploy.py deploy 100 --reviewer" in s for s in res["next_steps"]))
        self.api.update_status.assert_not_called()
        self.assertNotIn("deploy_requested", self.state()["review"])

    def test_yes_moves_to_devops_staging_and_records_the_request_without_triggering_anything(self):
        res = self.go(yes=True)
        self.assertEqual((res["status"], res["status_now"]), ("DEPLOY_REQUESTED", "DevOps Staging"))
        self.api.update_status.assert_called_once_with("100", "DevOps Staging")
        self.api.assign_and_move.assert_not_called()  # it stays with the reviewer
        requested = self.state()["review"]["deploy_requested"]
        self.assertEqual(requested["head"], A)
        self.assertEqual(self.state()["review"]["stage"], "R6-deploy")

    def test_the_db_companion_follows_and_its_head_is_checked(self):
        state = approved_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["approved_db_head"] = B
        self.write_state(state)
        self.companion.return_value = {"db_ticket": 200, "moved": True, "to": "DevOps Staging"}
        res = self.go(yes=True)
        self.assertTrue(res["db_companion"]["moved"])
        self.assertEqual(self.companion.call_args.args[2:], ("Dev Done", "DevOps Staging"))
        self.assertEqual(self.state()["review"]["deploy_requested"]["db_head"], B)

    def test_ungated_files_are_shown_in_the_preview_and_the_result(self):
        found = {"count": 2, "folders": ["AI_DEV_ENV"], "files": ["AI_DEV_ENV/a.py", "AI_DEV_ENV/b.py"], "token": "abc12345"}
        with mock.patch.object(review, "ungated_files", return_value=found):
            preview = self.go()
            moved = self.go(yes=True)
        self.assertEqual(preview["ungated_files"], {"wf": found})
        self.assertIn("--accept-ungated", preview["ungated_note"])
        self.assertEqual((moved["status"], moved["ungated_files"]), ("DEPLOY_REQUESTED", {"wf": found}))

    def test_no_ungated_files_adds_nothing_to_the_preview(self):
        with mock.patch.object(review, "ungated_files", return_value={}):
            res = self.go()
        self.assertNotIn("ungated_files", res)

    def test_the_db_branch_is_checked_against_the_db_files(self):
        state = approved_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["approved_db_head"] = B
        self.write_state(state)
        with mock.patch.object(review, "ungated_files", side_effect=lambda n, s, db=False: {"count": 1, "token": n} if db else {}) as lookup:
            res = self.go()
        self.assertEqual(res["ungated_files"], {"db": {"count": 1, "token": "200"}})
        self.assertIn(mock.call("200", mock.ANY, db=True), lookup.call_args_list)

    def test_a_push_after_the_approval_voids_it(self):
        self.tips["FB_100"] = ("OK", B)
        res = self.go(yes=True)
        self.assertEqual((res["status"], res["repo"]), ("APPROVAL_STALE", "wf"))
        self.api.update_status.assert_not_called()

    def test_a_moved_db_branch_voids_it_too(self):
        state = approved_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["approved_db_head"] = B
        self.write_state(state)
        self.tips["FB_200"] = ("OK", A)
        res = self.go(yes=True)
        self.assertEqual((res["status"], res["repo"]), ("APPROVAL_STALE", "db"))
        self.api.update_status.assert_not_called()

    def test_fetch_problems_are_reported(self):
        self.tips["FB_100"] = ("FETCH_FAILED", "offline")
        self.assertEqual(self.go(yes=True)["status"], "FETCH_FAILED")
        self.api.update_status.assert_not_called()

    def test_an_unapproved_review_cannot_be_deployed(self):
        for review in ({"stage": "R4-review", "author": "developer1"},
                       {"stage": "done", "author": "developer1", "verdict": {"verdict": "REJECT"}},
                       {"stage": "R6-deploy", "author": "developer1", "verdict": {"verdict": "APPROVE"}}):  # no approved_head
            self.write_state(approved_state(review=review))
            self.assertEqual(self.go(yes=True)["status"], "NOT_APPROVED", review)
        self.api.update_status.assert_not_called()

    def test_the_ticket_must_be_dev_done_and_mine(self):
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        self.assertEqual(self.go(yes=True)["status"], "WRONG_STATUS")
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s2"}
        self.assertEqual(self.go(yes=True)["status"], "NOT_ASSIGNED")
        self.api.update_status.assert_not_called()

    def test_a_second_request_is_reported_not_repeated(self):
        state = approved_state()
        state["review"]["deploy_requested"] = {"at": "2026-10-06T10:00:00Z", "head": A}
        self.write_state(state)
        self.api.get_ticket.return_value = {"status": "DevOps Staging", "assigned_to_id": "s1"}
        self.assertEqual(self.go(yes=True)["status"], "ALREADY_REQUESTED")
        self.api.update_status.assert_not_called()

    def test_the_caller_and_the_folder_are_checked(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.assertEqual(self.go(yes=True)["status"], "NOT_A_REVIEWER")
        self.assertEqual(self.go(yes=True, ticket="999")["status"], "NO_STATE")
        self.write_state({"ticket": "100", "role": "developer", "stage": "done"})
        self.assertEqual(self.go(yes=True)["status"], "NOT_A_REVIEW_FOLDER")
        state = approved_state()
        state["review"]["author"] = "reviewer1"
        self.write_state(state)
        self.assertEqual(self.go(yes=True)["status"], "SELF_REVIEW")
        self.api.update_status.assert_not_called()

    def test_the_move_must_be_in_the_reviewer_table(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewer_status_moves": [["Code Review", "Dev Done"]]}):
            self.assertEqual(self.go(yes=True)["status"], "NOT_ALLOWED")
        self.api.update_status.assert_not_called()

    def test_a_failed_move_is_not_retried_and_records_nothing(self):
        from assembla_api import AssemblaError
        self.api.update_status.side_effect = AssemblaError("Status update not applied")
        out = capture(lambda: dev_ticket.cmd_review_deploy(self.api, mock.Mock(ticket="100", yes=True)))
        self.assertEqual((out[0], out[1]["status"]), (1, "MOVE_FAILED"))
        self.assertEqual(self.api.update_status.call_count, 1)
        self.assertNotIn("deploy_requested", self.state()["review"])


class _StagingFixture(unittest.TestCase):
    """A fetched, approved and deploy-requested review of ticket 100; the Jenkins job runner and the git lookups are mocks."""

    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        state = approved_state()
        state["review"]["deploy_requested"] = {"at": "2026-10-06T10:00:00Z", "head": A}
        self.write_state(state)
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_ticket.return_value = {"number": 100, "status": "DevOps Staging", "assigned_to_id": "s1",
                                            "hierarchy_type": staging_deploy.STORY, "custom_fields": {}}
        self.run_job = mock.Mock(return_value=(0, "Single-ticket mode: selected #100\nApplications to build this cycle: ['c2mwf_admin']\n"))
        self.origin = {"FB_100": A}
        for patcher in (
                mock.patch.object(common, "PROJECT_ROOT", self.root),
                mock.patch.object(staging_deploy, "PROJECT_ROOT", self.root),
                mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1"]}),
                mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                mock.patch.dict(os.environ, {"JENKINS_REMOTE_USER": "u", "JENKINS_REMOTE_API_TOKEN": "tok"}),
                mock.patch.object(staging_deploy, "origin_sha", side_effect=lambda b, repo=None: self.origin.get(b, "")),
                mock.patch.object(staging_deploy, "local_sha", side_effect=lambda b, repo=None: self.origin.get(b, "")),
                mock.patch.object(staging_deploy, "changed_app_folders",
                                  return_value={"files": 1, "folders": ["c2mwf_admin"], "apps": ["c2mwf_admin"],
                                                "merged_into_master": False}),
                mock.patch.object(staging_deploy, "queue_position",
                                  side_effect=lambda api, n: {"status": "FIRST", "milestone": "R_CURRENT", "queue": [n], "ahead": []}),
                mock.patch.object(staging_deploy, "lock_holder", return_value=None),
                mock.patch.object(staging_deploy, "ungated_files", return_value={}),  # covered in test_ungated_files.py
                mock.patch.object(staging_deploy, "run_deploy_script", self.run_job),
                mock.patch.object(staging_deploy, "await_final_status", return_value="QA Queue"),
                mock.patch.object(staging_deploy, "db_ticket_status", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, yes=True, retry=False, ticket="100"):
        args = mock.Mock(ticket=ticket, yes=yes, requested=False, reviewer=True, retry=retry)
        return capture(lambda: staging_deploy._deploy(self.api, args))[1]

    def refuse(self, expected):
        res = self.go()
        self.assertEqual(res["status"], expected)
        self.run_job.assert_not_called()


class ReviewerStagingDeployTests(_StagingFixture):
    """staging_deploy.py deploy N --reviewer: every reviewer guard refuses BEFORE any trigger."""

    def test_preview_triggers_nothing(self):
        res = self.go(yes=False)
        self.assertEqual(res["status"], "PREVIEW")
        self.run_job.assert_not_called()

    def test_an_approved_requested_ticket_is_deployed_once_and_the_attempt_is_recorded(self):
        res = self.go()
        self.assertEqual((res["status"], res["final_status"]), ("DEPLOYED", "QA Queue"))
        self.run_job.assert_called_once()
        self.assertEqual(self.state()["deploy_attempt"]["build"], "local")

    def test_a_second_trigger_is_refused_and_a_retry_needs_the_flag(self):
        self.go()
        second = self.go()
        self.assertEqual(second["status"], "ALREADY_TRIGGERED")
        self.assertEqual(self.run_job.call_count, 1)
        retried = self.go(retry=True)
        self.assertEqual(retried["status"], "DEPLOYED")
        self.assertEqual(self.run_job.call_count, 2)
        self.assertEqual(len(self.state()["deploy_attempts"]), 1)  # the first attempt was archived, once

    def test_only_a_reviewer_folder_can_use_the_reviewer_mode(self):
        self.write_state({"ticket": "100", "role": "reviewer", "stage": "8-handoff"})
        self.refuse("NOT_A_REVIEW_FOLDER")

    def test_the_caller_must_be_a_configured_reviewer(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.refuse("NOT_A_REVIEWER")

    def test_an_unapproved_review_is_refused(self):
        for review in ({"stage": "R4-review", "author": "developer1"},
                       {"stage": "done", "author": "developer1", "verdict": {"verdict": "REJECT"}}):
            self.write_state(approved_state(review=review))
            self.refuse("NOT_APPROVED")

    def test_the_author_does_not_deploy_their_own_work(self):
        state = approved_state()
        state["review"].update(author="reviewer1", deploy_requested={"at": "t"})
        self.write_state(state)
        self.refuse("SELF_REVIEW")

    def test_the_deploy_request_must_have_been_made(self):
        self.write_state(approved_state())
        self.refuse("NOT_REQUESTED")

    def test_the_ticket_must_be_assigned_to_me_and_in_devops_staging(self):
        self.api.get_ticket.return_value = dict(self.api.get_ticket.return_value, assigned_to_id="s2")
        self.refuse("NOT_ASSIGNED")
        self.api.get_ticket.return_value = dict(self.api.get_ticket.return_value, assigned_to_id="s1", status="Dev Done")
        self.refuse("WRONG_STATUS")

    def test_a_push_after_the_approval_is_never_deployed(self):
        self.origin["FB_100"] = B
        res = self.go()
        self.assertEqual((res["status"], res["approved_head"], res["origin_head"]), ("APPROVAL_STALE", A[:12], B[:12]))
        self.run_job.assert_not_called()

    def test_the_unchanged_guards_still_apply(self):
        with mock.patch.object(staging_deploy, "queue_position",
                               return_value={"status": "QUEUED", "queue": ["99", "100"], "ahead": ["99"], "milestone": "m"}):
            self.refuse("QUEUED")
        with mock.patch.object(staging_deploy, "lock_holder", return_value=1234):
            self.refuse("BUSY")
        with mock.patch.object(staging_deploy, "local_sha", return_value=B):
            self.refuse("ORIGIN_DIFFERS")
        self.api.get_ticket.return_value = dict(self.api.get_ticket.return_value, hierarchy_type=0)
        self.refuse("NOT_STORY")

    def test_missing_jenkins_keys_stop_before_the_trigger(self):
        import os
        with mock.patch.dict(os.environ, {"JENKINS_REMOTE_USER": ""}):
            res = self.go()
        self.assertEqual(res["status"], "ERROR")
        self.run_job.assert_not_called()

    def test_the_cli_refuses_both_modes_at_once(self):
        with mock.patch("sys.argv", ["staging_deploy.py", "deploy", "100", "--requested", "--reviewer"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            staging_deploy.main()


class ReviewerDbDeployTests(_StagingFixture):
    """The DB ticket is deployed in its own second run, bound to approved_db_head, with its attempt kept in the owner's state."""

    def setUp(self):
        super().setUp()
        state = approved_state(db={"ticket": 200, "files": ["Database/C2M/SP/x.sql"]})
        state["review"].update(approved_db_head=B, deploy_requested={"at": "2026-10-06T10:00:00Z", "head": A, "db_head": B})
        self.write_state(state)
        main = dict(self.api.get_ticket.return_value)
        db_node = dev_ticket.CONFIG["db"]["node_value"]
        self.tickets = {"100": main, "200": {"number": 200, "status": "DevOps Staging", "hierarchy_type": staging_deploy.STORY,
                                             "custom_fields": {dev_ticket.CONFIG["deploy_attributes"]["node_field"]: db_node}}}
        self.api.get_ticket.side_effect = lambda n: self.tickets[str(n)]
        self.origin["FB_200"] = B
        self.run_job.return_value = (0, "Single-ticket mode: selected #200\nApplications to build this cycle: []\n")

    def test_preview_triggers_nothing(self):
        res = self.go(yes=False, ticket="200")
        self.assertEqual((res["status"], res["is_db_ticket"], res["owner"]), ("PREVIEW", True, "100"))
        self.run_job.assert_not_called()

    def test_the_db_ticket_deploys_and_its_attempt_is_kept_in_the_owners_state(self):
        res = self.go(ticket="200")
        self.assertEqual((res["status"], res["final_status"]), ("DEPLOYED_NO_APPS", "QA Queue"))  # SQL only: no app is built
        self.run_job.assert_called_once()
        state = self.state()
        self.assertEqual(state["db_deploy_attempt"]["build"], "local")
        self.assertNotIn("deploy_attempt", state)

    def test_the_main_ticket_and_its_db_ticket_are_two_separate_runs(self):
        self.run_job.return_value = (0, "Single-ticket mode: selected #100\nApplications to build this cycle: ['c2mwf_admin']\n")
        self.assertEqual(self.go(ticket="100")["status"], "DEPLOYED")
        self.run_job.return_value = (0, "Single-ticket mode: selected #200\nApplications to build this cycle: []\n")
        self.assertEqual(self.go(ticket="200")["status"], "DEPLOYED_NO_APPS")
        self.assertEqual(self.run_job.call_count, 2)
        state = self.state()
        self.assertIn("deploy_attempt", state)
        self.assertIn("db_deploy_attempt", state)

    def test_one_trigger_per_db_ticket_and_a_retry_archives_only_the_db_attempt(self):
        self.go(ticket="200")
        self.assertEqual(self.go(ticket="200")["status"], "ALREADY_TRIGGERED")
        self.assertEqual(self.run_job.call_count, 1)
        self.assertEqual(self.go(ticket="200", retry=True)["status"], "DEPLOYED_NO_APPS")
        self.assertEqual(self.run_job.call_count, 2)
        state = self.state()
        self.assertEqual(len(state["db_deploy_attempts"]), 1)
        self.assertNotIn("deploy_attempts", state)

    def test_a_moved_db_branch_is_never_deployed(self):
        self.origin["FB_200"] = A
        res = self.go(ticket="200")
        self.assertEqual((res["status"], res["approved_head"], res["origin_head"]), ("APPROVAL_STALE", B[:12], A[:12]))
        self.run_job.assert_not_called()

    def test_the_db_head_is_looked_up_in_the_db_repo(self):
        self.go(yes=False, ticket="200")
        repos = [c.args[1] if len(c.args) > 1 else c.kwargs.get("repo") for c in staging_deploy.origin_sha.call_args_list
                 if c.args[0] == "FB_200"]
        self.assertTrue(repos and all(r == staging_deploy.DB_REPO for r in repos), repos)

    def test_a_missing_approved_db_head_cannot_pass(self):
        state = self.state()
        del state["review"]["approved_db_head"]
        self.write_state(state)
        self.refuse_db("APPROVAL_STALE")

    def refuse_db(self, expected):
        res = self.go(ticket="200")
        self.assertEqual(res["status"], expected)
        self.run_job.assert_not_called()

    def test_a_db_ticket_whose_owner_is_not_approved_or_not_requested_is_refused(self):
        state = self.state()
        state["review"]["verdict"] = {"verdict": "REJECT"}
        self.write_state(state)
        self.refuse_db("NOT_APPROVED")
        state["review"]["verdict"] = {"verdict": "APPROVE"}
        del state["review"]["deploy_requested"]
        self.write_state(state)
        self.refuse_db("NOT_REQUESTED")

    def test_a_db_ticket_with_no_review_owner_is_refused(self):
        state = self.state()
        del state["db"]
        self.write_state(state)
        self.refuse_db("NOT_A_REVIEW_FOLDER")

    def test_the_other_reviewer_guards_apply_to_the_db_ticket_too(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.refuse_db("NOT_A_REVIEWER")
        state = self.state()
        state["review"]["author"] = "reviewer1"
        self.write_state(state)
        self.refuse_db("SELF_REVIEW")

    def test_the_db_ticket_must_be_in_devops_staging_and_first_in_the_queue(self):
        self.tickets["200"]["status"] = "Dev Done"
        self.refuse_db("WRONG_STATUS")
        self.tickets["200"]["status"] = "DevOps Staging"
        with mock.patch.object(staging_deploy, "queue_position",
                               return_value={"status": "QUEUED", "queue": ["100", "200"], "ahead": ["100"], "milestone": "m"}):
            self.refuse_db("QUEUED")


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        self.write_state(approved_state())
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        self.companion = mock.Mock(return_value=None)
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1"]}),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(common, "move_db_companion", self.companion)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, yes=False, ticket="100"):
        return capture(lambda: dev_ticket.cmd_review_withdraw(self.api, mock.Mock(ticket=ticket, yes=yes)))[1]

    def test_preview_changes_nothing(self):
        res = self.go()
        self.assertEqual((res["status"], res["approved_head"]), ("PREVIEW", A[:12]))
        self.api.update_status.assert_not_called()
        self.assertEqual(self.state(), approved_state())

    def test_withdrawing_from_dev_done_moves_back_and_clears_the_approval(self):
        res = self.go(yes=True)
        self.assertEqual((res["status"], res["status_now"], res["review_stage"]), ("WITHDRAWN", "Code Review", "R4-review"))
        self.api.update_status.assert_called_once_with("100", "Code Review")
        self.api.assign_and_move.assert_not_called()  # it stays with the reviewer
        review = self.state()["review"]
        for key in ("approved_head", "approved_db_head", "deploy_requested", "verdict"):
            self.assertNotIn(key, review)
        self.assertEqual((review["stage"], review["withdrawn"]["head"], review["withdrawn"]["was"]), ("R4-review", A, "Dev Done"))
        self.assertEqual(review["withdrawn"]["verdict"]["verdict"], "APPROVE")  # kept for the record

    def test_a_requested_but_not_deployed_ticket_can_be_withdrawn_from_devops_staging(self):
        state = approved_state()
        state["review"]["deploy_requested"] = {"at": "2026-10-06T10:00:00Z", "head": A}
        self.write_state(state)
        self.api.get_ticket.return_value = {"status": "DevOps Staging", "assigned_to_id": "s1"}
        res = self.go(yes=True)
        self.assertEqual(res["status"], "WITHDRAWN")
        self.assertNotIn("deploy_requested", self.state()["review"])

    def test_the_db_companion_follows_from_the_same_status(self):
        self.write_state(approved_state(db={"ticket": 200, "files": ["x.sql"]}))
        self.companion.return_value = {"db_ticket": 200, "moved": True, "to": "Code Review"}
        res = self.go(yes=True)
        self.assertTrue(res["db_companion"]["moved"])
        self.assertEqual(self.companion.call_args.args[2:], ("Dev Done", "Code Review"))

    def test_after_a_withdrawal_the_deploy_request_and_the_deploy_are_refused(self):
        self.go(yes=True)
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        with mock.patch.object(review, "fetch_origin_branch", return_value=("OK", A)):
            res = capture(lambda: dev_ticket.cmd_review_deploy(self.api, mock.Mock(ticket="100", yes=True)))[1]
        self.assertEqual(res["status"], "NOT_APPROVED")

    def test_nothing_to_withdraw_without_an_approval(self):
        for review in ({"stage": "R4-review", "author": "developer1"},
                       {"stage": "done", "author": "developer1", "verdict": {"verdict": "REJECT"}},
                       {"stage": "R6-deploy", "author": "developer1", "verdict": {"verdict": "APPROVE"}}):
            self.write_state(approved_state(review=review))
            self.assertEqual(self.go(yes=True)["status"], "NOT_APPROVED", review)
        self.api.update_status.assert_not_called()

    def test_a_triggered_deploy_cannot_be_withdrawn(self):
        for key in ("deploy_attempt", "db_deploy_attempt"):
            state = approved_state()
            state[key] = {"build": "local", "at": "2026-10-06T11:00:00Z"}
            self.write_state(state)
            self.assertEqual(self.go(yes=True)["status"], "ALREADY_DEPLOYED", key)
        self.api.update_status.assert_not_called()

    def test_only_an_approved_ticket_in_dev_done_or_devops_staging_can_be_withdrawn(self):
        for status in ("Code Review", "QA Queue", "Staging Code Merged", "Dev Queue"):
            self.api.get_ticket.return_value = {"status": status, "assigned_to_id": "s1"}
            self.assertEqual(self.go(yes=True)["status"], "WRONG_STATUS", status)
        self.api.update_status.assert_not_called()

    def test_guards_on_the_caller_the_folder_and_the_author(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.assertEqual(self.go(yes=True)["status"], "NOT_A_REVIEWER")
        self.assertEqual(self.go(yes=True, ticket="999")["status"], "NO_STATE")
        self.write_state({"ticket": "100", "role": "developer", "stage": "done"})
        self.assertEqual(self.go(yes=True)["status"], "NOT_A_REVIEW_FOLDER")
        state = approved_state()
        state["review"]["author"] = "reviewer1"
        self.write_state(state)
        self.assertEqual(self.go(yes=True)["status"], "SELF_REVIEW")
        self.write_state(approved_state())
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s2"}
        self.assertEqual(self.go(yes=True)["status"], "NOT_ASSIGNED")
        self.api.update_status.assert_not_called()

    def test_the_move_must_be_in_the_reviewer_table(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewer_status_moves": [["Code Review", "Dev Done"]]}):
            self.assertEqual(self.go(yes=True)["status"], "NOT_ALLOWED")
        self.api.update_status.assert_not_called()

    def test_a_failed_move_keeps_the_approval_and_is_not_retried(self):
        from assembla_api import AssemblaError
        self.api.update_status.side_effect = AssemblaError("Status update not applied")
        out = capture(lambda: dev_ticket.cmd_review_withdraw(self.api, mock.Mock(ticket="100", yes=True)))
        self.assertEqual((out[0], out[1]["status"]), (1, "MOVE_FAILED"))
        self.assertEqual(self.api.update_status.call_count, 1)
        self.assertEqual(self.state(), approved_state())

    def test_the_config_has_the_withdraw_moves(self):
        moves = dev_ticket.CONFIG["reviewer_status_moves"]
        self.assertIn(["Dev Done", "Code Review"], moves)
        self.assertIn(["DevOps Staging", "Code Review"], moves)


if __name__ == "__main__":
    unittest.main()
