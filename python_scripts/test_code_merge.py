"""Staging deploy through DevOps' Code_Merge job (staging_deploy.adapter = "code_merge", the C2M CI profile).
Jenkins and Assembla are mocked; the console samples are shortened copies of real runs, with no secrets."""
import unittest
from unittest import mock

import code_merge
import staging_deploy
from test_review_deploy import _StagingFixture, capture

PICKED_OK = """C:\\Repository\\DevOps>python -u main\\mergeRequest.py -jn Code_Merge -bn 63640
19024 Release
All tickets have the same release type.
Creating merge request for ticket # 19024
DEBUG node: GenAI
Merge request successfully created for ticket #  19024
Merge Reqest success for ID: 8663317.
=== Get a list of what to build. ===
['EmbeddingService', 'GenAIService']
Build for EmbeddingService is successful.
Build for GenAIService is successful.
GenAIService ['GENAI_PORT=8070', 'SOME_SECRET=do-not-copy']
Finished: SUCCESS
"""
PICKED_FAILED = """Creating merge request for ticket # 100
Merge Reqest success for ID: 1.
=== Get a list of what to build. ===
['GenAIService']
Build for GenAIService is failed.
Build failed for 100. Will reset to the previous commit.
Failed application: ['GenAIService']
Finished: SUCCESS
"""
NOTHING = """Nothing to create merge request. exiting script.
=== Get a list of what to build. ===
[]
Finished: SUCCESS
"""


class ParseConsoleTests(unittest.TestCase):
    def test_a_successful_run(self):
        facts = code_merge.parse_console(PICKED_OK)
        self.assertEqual(facts["tickets"], ["19024"])
        self.assertEqual(facts["apps"], ["EmbeddingService", "GenAIService"])
        self.assertEqual(facts["failed_apps"], [])
        self.assertEqual(facts["finished"], "SUCCESS")
        self.assertNotIn("do-not-copy", repr(facts))  # env lines (secrets) never come back

    def test_a_failed_build_is_reset(self):
        facts = code_merge.parse_console(PICKED_FAILED)
        self.assertEqual((facts["failed_apps"], facts["reset_for"]), (["GenAIService"], ["100"]))

    def test_nothing_to_merge(self):
        facts = code_merge.parse_console(NOTHING)
        self.assertTrue(facts["nothing_to_merge"])
        self.assertEqual((facts["tickets"], facts["apps"]), ([], []))


class QueueTests(unittest.TestCase):
    def api(self, *tickets):
        api = mock.Mock()
        api.get_active_tickets.return_value = [{"number": n, "status": s} for n, s in tickets]
        return api

    def test_alone(self):
        info = code_merge.queue_position(self.api((100, "DevOps Staging"), (5, "Dev Queue")), "DevOps Staging", "100")
        self.assertEqual(info["status"], "ALONE")

    def test_shared_names_the_others_with_a_token_bound_to_them(self):
        info = code_merge.queue_position(self.api((100, "DevOps Staging"), (90, "DevOps Staging")), "DevOps Staging", "100")
        self.assertEqual((info["status"], info["others"]), ("SHARED", ["90"]))
        self.assertNotEqual(info["token"], code_merge.queue_token(["90", "91"]))

    def test_not_in_queue(self):
        self.assertEqual(code_merge.queue_position(self.api((90, "DevOps Staging")), "DevOps Staging", "100")["status"],
                         "NOT_IN_QUEUE")


class FakeJenkins:
    def __init__(self, next_number=10, busy=None, console=PICKED_OK):
        self.next_number, self.busy_state, self.console = next_number, busy, console
        self.posts = []

    def _get_json(self, path):
        if "inQueue" in path:
            return self.busy_state or {"inQueue": False, "lastBuild": {"number": 9, "building": False}}
        if "nextBuildNumber" in path:
            return {"nextBuildNumber": self.next_number}
        if "builds[number]" in path:
            return {"builds": [{"number": 11}, {"number": 10}, {"number": 9}]} if self.posts else {"builds": [{"number": 9}]}
        if "building,result" in path:
            return {"building": False, "result": "SUCCESS"}
        raise AssertionError(path)

    def _fetch_crumb(self):
        return ("Jenkins-Crumb", "c")

    def _request(self, path, method="GET", extra_headers=None, data=None):
        if method == "POST":
            self.posts.append((path, extra_headers))
            return 201, ""
        return 200, self.console

    def job_exists(self, name):
        return True


class CodeMergeJobTests(unittest.TestCase):
    def test_trigger_returns_the_first_new_build_and_sends_the_crumb(self):
        jenkins = FakeJenkins()
        job = code_merge.CodeMergeJob(jenkins, "Code_Merge", sleep=lambda s: None)
        self.assertEqual(job.trigger(), 10)
        self.assertEqual(jenkins.posts, [("/job/Code_Merge/build?delay=0sec", {"Jenkins-Crumb": "c"})])

    def test_wait_parses_the_finished_build(self):
        facts = code_merge.CodeMergeJob(FakeJenkins(), "Code_Merge", sleep=lambda s: None).wait(10)
        self.assertEqual((facts["build"], facts["result"], facts["tickets"]), (10, "SUCCESS", ["19024"]))

    def test_busy(self):
        job = code_merge.CodeMergeJob(FakeJenkins(busy={"lastBuild": {"number": 9, "building": True}}), "Code_Merge")
        self.assertEqual(job.busy(), {"running_build": 9})
        self.assertIsNone(code_merge.CodeMergeJob(FakeJenkins(), "Code_Merge").busy())


class CodeMergeDeployTests(_StagingFixture):
    """`deploy N --reviewer` with the code_merge adapter: the usual guards, then one trigger of Code_Merge."""

    def setUp(self):
        super().setUp()
        self.queue = {"status": "ALONE", "queue": ["100"]}
        self.job = mock.Mock()
        self.job.busy.return_value = None
        self.job.trigger.return_value = 77
        self.job.wait.return_value = dict(code_merge.parse_console(PICKED_OK.replace("19024", "100")), build=77,
                                          result="SUCCESS")
        for patcher in (mock.patch.object(staging_deploy, "CODE_MERGE", True),
                        mock.patch.dict(staging_deploy.DEPLOY, {"job": "Code_Merge"}),
                        mock.patch.object(code_merge, "queue_position", side_effect=lambda api, s, n: dict(self.queue)),
                        mock.patch.object(staging_deploy, "code_merge_job", return_value=self.job)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def go(self, yes=True, accept=None, retry=False):
        args = mock.Mock(ticket="100", yes=yes, requested=False, reviewer=True, retry=retry, accept_queue=accept)
        return capture(lambda: staging_deploy._deploy(self.api, args))[1]

    def test_preview_triggers_nothing(self):
        res = self.go(yes=False)
        self.assertEqual((res["status"], res["job"]), ("PREVIEW", "Code_Merge"))
        self.job.trigger.assert_not_called()

    def test_deployed_when_the_run_took_our_ticket_and_it_reached_qa_queue(self):
        res = self.go()
        self.assertEqual((res["status"], res["build"], res["apps"]), ("DEPLOYED", 77, ["EmbeddingService", "GenAIService"]))
        self.job.trigger.assert_called_once()
        self.assertEqual(self.state()["deploy_attempt"]["build"], 77)
        self.run_job.assert_not_called()  # never the WF script

    def test_other_tickets_in_staging_need_an_explicit_acceptance(self):
        self.queue = {"status": "SHARED", "queue": ["90", "100"], "others": ["90"], "token": "tok90"}
        res = self.go()
        self.assertEqual((res["status"], res["others"], res["token"]), ("SHARED_QUEUE", ["90"], "tok90"))
        self.job.trigger.assert_not_called()
        self.assertEqual(self.go(accept="tok90")["status"], "DEPLOYED")

    def test_a_running_job_refuses(self):
        self.job.busy.return_value = {"running_build": 76}
        res = self.go()
        self.assertEqual((res["status"], res["running_build"]), ("BUSY", 76))
        self.job.trigger.assert_not_called()

    def test_the_run_took_another_ticket(self):
        self.job.wait.return_value = dict(code_merge.parse_console(PICKED_OK), build=77, result="SUCCESS")
        res = self.go()
        self.assertEqual((res["status"], res["selected"]), ("DEPLOYED_OTHER_TICKET", ["19024"]))

    def test_the_job_skipped_our_ticket(self):
        self.job.wait.return_value = dict(code_merge.parse_console(NOTHING), build=77, result="SUCCESS")
        self.assertEqual(self.go()["status"], "NOT_PICKED")

    def test_a_failed_build_is_reported_with_its_apps(self):
        self.job.wait.return_value = dict(code_merge.parse_console(PICKED_FAILED), build=77, result="SUCCESS")
        with mock.patch.object(staging_deploy, "await_final_status", return_value="Build Failed"):
            res = self.go()
        self.assertEqual((res["status"], res["failed_apps"], res["reset"]), ("DEPLOY_FAILED", ["GenAIService"], True))

    def test_never_triggered_twice(self):
        self.go()
        res = self.go()
        self.assertEqual(res["status"], "ALREADY_TRIGGERED")
        self.job.trigger.assert_called_once()


if __name__ == "__main__":
    unittest.main()
