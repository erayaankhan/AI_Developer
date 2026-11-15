"""Improvements item 20: files a branch brings to master that no gates run covered. The helper (git faked) and the
deploy's refusal / acceptance token. Nothing is deployed."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dev_ticket
import staging_deploy
from develop_ticket import common
from develop_ticket.ungated import ungated_files

STATUS = dev_ticket.STATUS
TIP = "a" * 40


class FakeGit:
    def __init__(self, diff="", tip=TIP, fail=None):
        self.diff, self.tip, self.fail, self.calls = diff, tip, fail, []

    def __call__(self, *args, check=True, repo=None, **_):
        self.calls.append((args, repo))
        if self.fail and args[0] == self.fail:
            raise RuntimeError(f"git {args[0]} failed")
        out = {"diff": self.diff, "rev-parse": self.tip + "\n"}.get(args[0], "")
        return SimpleNamespace(stdout=out, returncode=0)


def state(files, db_files=None):
    return {"gates": {"files": files}, "db": {"ticket": 200, "files": db_files or []} if db_files is not None else {}}


class HelperTests(unittest.TestCase):
    def found(self, diff, st, db=False, **git):
        fake = FakeGit(diff, **git)
        with mock.patch.object(common, "git", fake):
            return ungated_files("100" if not db else "200", st, db), fake

    def test_a_branch_of_only_gated_files_has_nothing_extra(self):
        res, _ = self.found("a/x.ts\na/y.ts\n", state(["a/x.ts", "a/y.ts"]))
        self.assertEqual(res, {})

    def test_files_beyond_the_gated_ones_are_listed_with_their_folders(self):
        res, _ = self.found("c2mwf_admin/x.ts\nAI_DEV_ENV/a.py\nAI_DEV_ENV/b.py\n", state(["c2mwf_admin/x.ts"]))
        self.assertEqual((res["count"], res["folders"]), (2, ["AI_DEV_ENV"]))
        self.assertEqual(res["files"], ["AI_DEV_ENV/a.py", "AI_DEV_ENV/b.py"])

    def test_documentation_files_do_not_count(self):
        res, _ = self.found("README.md\ndocs/guide.html\nnotes.txt\n", state([]))
        self.assertEqual(res, {})

    def test_backslash_paths_in_the_gated_list_still_match(self):
        res, _ = self.found("a/x.ts\n", state(["a\\x.ts"]))
        self.assertEqual(res, {})

    def test_only_the_first_twenty_are_shown_but_all_are_counted(self):
        diff = "".join(f"AI_DEV_ENV/f{i:02}.py\n" for i in range(25))
        res, _ = self.found(diff, state([]))
        self.assertEqual((res["count"], len(res["files"])), (25, 20))

    def test_the_token_follows_the_tip_and_the_list(self):
        one, _ = self.found("x/a.py\n", state([]))
        same, _ = self.found("x/a.py\n", state([]))
        pushed, _ = self.found("x/a.py\n", state([]), tip="b" * 40)
        more, _ = self.found("x/a.py\nx/b.py\n", state([]))
        self.assertEqual(one["token"], same["token"])
        self.assertNotEqual(one["token"], pushed["token"])
        self.assertNotEqual(one["token"], more["token"])
        self.assertEqual(len(one["token"]), 8)

    def test_a_db_ticket_is_compared_with_its_db_files_in_the_db_repo(self):
        res, fake = self.found("x.sql\ny.sql\n", state(["unrelated.ts"], db_files=["x.sql"]), db=True)
        self.assertEqual(res["files"], ["y.sql"])
        self.assertTrue(all(repo == common.DB_REPO for _args, repo in fake.calls))
        base = dev_ticket.CONFIG["db"]["base_branch"]
        self.assertTrue(any(f"origin/{base}...origin/FB_200" in args for args, _repo in fake.calls))

    def test_a_git_failure_is_an_error_not_an_empty_list(self):
        res, _ = self.found("", state([]), fail="diff")
        self.assertIn("error", res)
        self.assertNotIn("count", res)


class DeployTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "DEV_TICKETS" / "100"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"ticket": "100", "role": "reviewer", "stage": "8-handoff"}),
                                           encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["staging"],
                                            "hierarchy_type": staging_deploy.STORY, "custom_fields": {}}
        self.found = {"count": 1, "folders": ["AI_DEV_ENV"], "files": ["AI_DEV_ENV/a.py"], "token": "tok12345"}
        self.run_job = mock.Mock(return_value=(0, "Single-ticket mode: selected #100\nApplications to build this cycle: []\n"))
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(staging_deploy, "PROJECT_ROOT", self.root),
                        mock.patch.dict("os.environ", {"JENKINS_REMOTE_USER": "u", "JENKINS_REMOTE_API_TOKEN": "tok"}),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(staging_deploy, "origin_sha", return_value=TIP),
                        mock.patch.object(staging_deploy, "local_sha", return_value=TIP),
                        mock.patch.object(staging_deploy, "handoff_evidence_problem", return_value=None),
                        mock.patch.object(staging_deploy, "changed_app_folders",
                                          return_value={"files": 1, "folders": ["AI_DEV_ENV"], "apps": [],
                                                        "merged_into_master": False}),
                        mock.patch.object(staging_deploy, "ungated_files", side_effect=lambda *a, **k: self.found),
                        mock.patch.object(staging_deploy, "queue_position",
                                          return_value={"status": "FIRST", "milestone": "M", "queue": ["100"], "ahead": []}),
                        mock.patch.object(staging_deploy, "lock_holder", return_value=None),
                        mock.patch.object(staging_deploy, "run_deploy_script", self.run_job),
                        mock.patch.object(staging_deploy, "await_final_status", return_value="QA Queue"),
                        mock.patch.object(staging_deploy, "db_ticket_status", return_value=None),
                        mock.patch.object(staging_deploy, "_record_attempt")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def deploy(self, **kw):
        out = io.StringIO()
        args = SimpleNamespace(ticket="100", yes=True, requested=True, retry=False, reviewer=False, accept_ungated=None)
        vars(args).update(kw)
        with contextlib.redirect_stdout(out):
            code = staging_deploy._deploy(self.api, args)
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def test_the_preview_lists_the_ungated_files_and_triggers_nothing(self):
        code, res = self.deploy(yes=False)
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertEqual(res["ungated_files"], self.found)
        self.run_job.assert_not_called()

    def test_yes_without_the_token_is_refused_and_shows_the_token(self):
        _code, res = self.deploy()
        self.assertEqual(res["status"], "UNGATED_FILES")
        self.assertIn("--accept-ungated tok12345", res["message"])
        self.run_job.assert_not_called()

    def test_a_wrong_token_is_refused(self):
        _code, res = self.deploy(accept_ungated="old-token")
        self.assertEqual(res["status"], "UNGATED_FILES")
        self.run_job.assert_not_called()

    def test_the_right_token_lets_the_deploy_run_and_the_list_stays_in_the_result(self):
        _code, res = self.deploy(accept_ungated="tok12345")
        self.run_job.assert_called_once()
        self.assertEqual(res["ungated_files"], self.found)

    def test_nothing_ungated_needs_no_token(self):
        self.found = {}
        _code, res = self.deploy()
        self.run_job.assert_called_once()
        self.assertNotIn("ungated_files", res)

    def test_a_failed_lookup_stops_a_yes_but_still_previews(self):
        self.found = {"error": "RuntimeError: git diff failed"}
        _code, preview = self.deploy(yes=False)
        self.assertEqual(preview["status"], "PREVIEW")
        self.assertIn("ungated_check_error", preview)
        _code, res = self.deploy(accept_ungated="anything")
        self.assertEqual(res["status"], "UNGATED_CHECK_FAILED")
        self.run_job.assert_not_called()

    def test_the_low_path_is_checked_too(self):
        with mock.patch.object(staging_deploy, "low_path_problems", return_value=[]):
            _code, res = self.deploy(requested=False)
        self.assertEqual(res["status"], "UNGATED_FILES")


if __name__ == "__main__":
    unittest.main()
