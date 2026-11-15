"""Sonar / Snyk: `dt quality` (developer's last step), the hand-off guard, `dt quality-bounce` and the rework it leads to.
SonarQube, Snyk, Jenkins and Assembla are faked; the console and gate samples are shortened copies of real ones."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
import quality
import test_rework as trw
from develop_ticket import common, quality_cmds
from test_review_deploy import capture

APPS = {"DeviceMgmtWebApp": {"sonar": {"project_key": "DeviceMgmtWebApp", "scanner": "cli", "sources": "src"}},
        "DeviceMgmtAPI": {"sonar": {"project_key": "DeviceMgmtAPI", "scanner": "dotnet"},
                          "snyk": {"file": "obj/project.assets.json"}}}
CFG = {"quality": {"enabled": True, "sonar_host": "https://sonar.example", "apps": APPS,
                   "bounce": {"enabled": True, "failed_statuses": ["Build Failed"], "merge_jobs": ["Code_Merge_Launchpad_CMGMT"]}}}

GATE_JSON = ('{"projectStatus":{"status":"ERROR","conditions":[{"status":"OK","metricKey":"new_coverage","comparator":"LT",'
             '"errorThreshold":"80","actualValue":"0.0"},{"status":"ERROR","metricKey":"new_violations","comparator":"GT",'
             '"errorThreshold":"0","actualValue":"2"}]}}')
SONAR_FAILED_CONSOLE = f"""[CMGMT] $ sonar-scanner.bat -Dsonar.host.url=http://x ******** -Dsonar.projectKey=DeviceMgmtWebApp -Dsonar.sources=src
C:\\Renesas\\Repository\\CMGMT>curl -u squ_notarealtoken: "https://sonar/api/qualitygates/project_status?projectKey=DeviceMgmtWebApp"
C:\\Renesas\\Repository\\CMGMT>set RAW_JSON={GATE_JSON}
{GATE_JSON}
Quality Gate failed Exiting build.
Build step 'Execute Windows batch command' marked build as failure
Finished: FAILURE
"""
SNYK_FAILED_CONSOLE = """Quality Gate passed.
> snyk-win.exe test --json --severity-threshold=medium --file=C:\\x\\obj\\project.assets.json
Vulnerabilities found!
Result: 6 known vulnerabilities | 13 dependencies
ERROR: Snyk has detected security vulnerabilities in your project.
Finished: FAILURE
"""
COMPILE_FAILED_CONSOLE = "error TS2304: Cannot find name 'x'.\nFinished: FAILURE\n"


def sonar_issue(path, line, impact="MEDIUM", legacy="MAJOR", rule="ts:S1", key="DeviceMgmtWebApp__dev_me"):
    return {"component": f"{key}:{path}", "line": line, "severity": legacy, "rule": rule, "message": f"msg {rule}",
            "type": "CODE_SMELL", "impacts": [{"softwareQuality": "MAINTAINABILITY", "severity": impact}]}


class HelpersTests(unittest.TestCase):
    def test_touched_apps_are_the_configured_folders_holding_changed_files(self):
        files = ["DeviceMgmtWebApp/src/a.ts", "DeviceMgmtAPI.Tests/x.cs", "README.md", "DeviceMgmtAPI/Program.cs"]
        self.assertEqual(quality.touched_apps(CFG, files), ["DeviceMgmtAPI", "DeviceMgmtWebApp"])

    def test_dev_project_key_never_is_the_real_one(self):
        self.assertEqual(quality.dev_project_key("DeviceMgmtWebApp", "shamshad k"), "DeviceMgmtWebApp__dev_shamshad_k")

    def test_unified_zero_diff_gives_the_new_side_lines(self):
        diff = ("diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -3,0 +4,2 @@\n+a\n+b\n@@ -10 +12 @@\n-c\n+d\n"
                "diff --git a/gone.ts b/gone.ts\n--- a/gone.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(quality.parse_unified_zero(diff), {"x.ts": {4, 5, 12}})

    def test_severity_mapping_new_impacts_and_legacy(self):
        high = quality.issue_view(sonar_issue("src/a.ts", 3, impact="HIGH"), "DeviceMgmtWebApp__dev_me", "DeviceMgmtWebApp")
        low = quality.issue_view(sonar_issue("src/a.ts", 3, impact="LOW", legacy="MINOR"), "DeviceMgmtWebApp__dev_me", "DeviceMgmtWebApp")
        legacy_major = quality.issue_view(dict(sonar_issue("src/a.ts", 3), impacts=[]), "DeviceMgmtWebApp__dev_me", "")
        self.assertEqual((high["file"], high["blocking"], high["severity"]), ("DeviceMgmtWebApp/src/a.ts", True, "HIGH"))
        self.assertFalse(low["blocking"])
        self.assertTrue(legacy_major["blocking"])

    def test_only_issues_on_changed_lines_count(self):
        views = [{"file": "A/a.ts", "line": 5}, {"file": "A/a.ts", "line": 50}, {"file": "A/b.ts", "line": 1},
                 {"file": "A/a.ts", "line": None}]
        self.assertEqual(quality.on_changed_lines(views, {"A/a.ts": {5, 6}}),
                         [{"file": "A/a.ts", "line": 5}, {"file": "A/a.ts", "line": None}])

    def test_snyk_json_splits_fixable_and_unfixable(self):
        out = json.dumps({"vulnerabilities": [
            {"id": "SNYK-1", "title": "Prototype pollution", "severity": "high", "packageName": "lodash", "version": "4.17.4",
             "isUpgradable": True, "upgradePath": [False, "lodash@4.17.21"], "fixedIn": ["4.17.21"], "from": ["app", "lodash@4.17.4"]},
            {"id": "SNYK-1", "packageName": "lodash", "version": "4.17.4", "isUpgradable": True},  # duplicate path
            {"id": "SNYK-2", "title": "ReDoS", "severity": "medium", "packageName": "x", "version": "1.0.0",
             "isUpgradable": False, "isPatchable": False}]})
        parsed = quality.parse_snyk(out)
        self.assertEqual([v["id"] for v in parsed["fixable"]], ["SNYK-1"])
        self.assertEqual(parsed["fixable"][0]["upgrade_to"], "lodash@4.17.21")
        self.assertEqual([v["id"] for v in parsed["unfixable"]], ["SNYK-2"])


class ConsoleTests(unittest.TestCase):
    def test_sonar_gate_failure_with_its_conditions_and_no_secret(self):
        facts = quality.classify_build_console(SONAR_FAILED_CONSOLE)
        self.assertEqual(facts["reasons"], ["SONAR"])
        self.assertEqual(facts["gate_conditions"], [{"metric": "new_violations", "actual": "2", "threshold": "0", "comparator": "GT"}])
        self.assertEqual(facts["sonar_project"], "DeviceMgmtWebApp")
        self.assertNotIn("squ_", json.dumps(facts))

    def test_snyk_failure(self):
        facts = quality.classify_build_console(SNYK_FAILED_CONSOLE)
        self.assertEqual((facts["reasons"], facts["snyk"]), (["SNYK"], {"vulnerabilities": 6, "dependencies": 13}))

    def test_anything_else_is_other(self):
        self.assertEqual(quality.classify_build_console(COMPILE_FAILED_CONSOLE)["reasons"], ["OTHER"])


class FakeSonar:
    def __init__(self, issues):
        self._issues = issues

    def wait_task(self, task_id):
        return "SUCCESS"

    def issues(self, key, new_code_only=False):
        return self._issues


class RunSonarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "DeviceMgmtWebApp" / ".scannerwork").mkdir(parents=True)
        (self.repo / "DeviceMgmtWebApp" / ".scannerwork" / "report-task.txt").write_text("ceTaskId=T1\n")
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_scans_into_the_dev_project_with_the_token_in_the_environment_only(self):
        issues = [sonar_issue("src/a.ts", 5, "HIGH"), sonar_issue("src/a.ts", 6, "LOW", "MINOR"),
                  sonar_issue("src/a.ts", 99, "HIGH"), sonar_issue("src/other.ts", 1, "HIGH")]
        res = quality.run_sonar("DeviceMgmtWebApp", APPS["DeviceMgmtWebApp"], self.repo, "me", "https://sonar.example",
                                "TOKEN123", {"DeviceMgmtWebApp/src/a.ts": {5, 6}}, client=FakeSonar(issues), runner=self.runner)
        self.assertEqual((res["status"], res["dev_project"]), ("FAIL", "DeviceMgmtWebApp__dev_me"))
        self.assertEqual([(i["line"], i["severity"]) for i in res["blocking"]], [(5, "HIGH")])
        self.assertEqual([i["line"] for i in res["low"]], [6])
        command, kwargs = self.calls[0]
        self.assertIn("-Dsonar.projectKey=DeviceMgmtWebApp__dev_me", command)
        self.assertNotIn("-Dsonar.projectKey=DeviceMgmtWebApp", command)
        self.assertFalse(any("TOKEN123" in part for part in command))
        self.assertEqual(kwargs["env"]["SONAR_TOKEN"], "TOKEN123")

    def test_only_low_issues_pass(self):
        res = quality.run_sonar("DeviceMgmtWebApp", APPS["DeviceMgmtWebApp"], self.repo, "me", "https://s", "t",
                                {"DeviceMgmtWebApp/src/a.ts": {6}}, client=FakeSonar([sonar_issue("src/a.ts", 6, "LOW", "MINOR")]),
                                runner=self.runner)
        self.assertEqual((res["status"], len(res["low"])), ("PASS", 1))

    def test_dotnet_needs_its_scanner_installed(self):
        res = quality.run_sonar("DeviceMgmtAPI", APPS["DeviceMgmtAPI"], self.repo, "me", "https://s", "t", {},
                                runner=self.runner, tool_ok=lambda: False)
        self.assertEqual(res["status"], "TOOL_MISSING")
        self.assertEqual(self.calls, [])

    def test_an_unknown_scanner_is_left_to_jenkins(self):
        res = quality.run_sonar("x", {"sonar": {"scanner": "gradle"}}, self.repo, "me", "https://s", "t", {})
        self.assertEqual(res["status"], "NOT_SUPPORTED")

    def test_maven_compiles_and_runs_the_sonar_plugin_with_the_token_only_in_maven_opts(self):
        seen = {}

        def runner(cmd, cwd=None, env=None, **_):
            seen.update(cmd=cmd, cwd=cwd, opts=env.get("MAVEN_OPTS"))
            return mock.Mock(returncode=0, stdout="", stderr="")
        client = mock.Mock()
        client.issues.return_value = []
        res = quality.run_sonar("timelineretrieval", {"sonar": {"project_key": "timelineretrieval", "scanner": "maven"}},
                                self.repo, "me", "https://s", "secret-t", {}, client=client, runner=runner,
                                tool_ok=lambda: True)
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["dev_project"], "timelineretrieval__dev_me")
        self.assertIn(quality.SONAR_MAVEN_PLUGIN, seen["cmd"])
        self.assertIn("-DskipTests", seen["cmd"])
        self.assertIn("-Dsonar.projectKey=timelineretrieval__dev_me", seen["cmd"])
        self.assertFalse(any("secret-t" in part for part in seen["cmd"]))
        self.assertIn("-Dsonar.token=secret-t", seen["opts"])
        self.assertTrue(seen["cwd"].endswith("timelineretrieval"))

    def test_maven_without_mvn_is_tool_missing(self):
        res = quality.run_sonar("x", {"sonar": {"scanner": "maven"}}, self.repo, "me", "https://s", "t", {},
                                tool_ok=lambda: False)
        self.assertEqual(res["status"], "TOOL_MISSING")

    def test_snyk_uses_the_apps_jenkins_threshold(self):
        self.assertIn("--severity-threshold=low", quality.snyk_command("pom.xml", "low"))
        self.assertIn("--severity-threshold=medium", quality.snyk_command("pom.xml"))
        self.assertIn("--severity-threshold=medium", quality.snyk_command("pom.xml", "bogus"))


class QualityCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        (self.folder / "state.json").write_text(json.dumps({"ticket": "100", "stage": "7-verify"}), encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"login": "me"}
        self.sonar = {"status": "PASS", "blocking": [], "low": []}
        self.snyk = {"status": "PASS", "fixable": [], "unfixable": []}
        import local_gates
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, CFG),
                        mock.patch.dict("os.environ", {"SONAR_TOKEN": "t", "SNYK_TOKEN": "s"}),
                        mock.patch.object(local_gates, "refresh_base", return_value=""),
                        mock.patch.object(local_gates, "changed_files",
                                          return_value=["DeviceMgmtWebApp/src/a.ts", "DeviceMgmtAPI/Program.cs"]),
                        mock.patch.object(local_gates, "diff_fingerprint", return_value="fp1"),
                        mock.patch.object(quality, "changed_lines", return_value={}),
                        mock.patch.object(quality, "run_sonar", side_effect=lambda app, *a, **k: dict(self.sonar, app=app)),
                        mock.patch.object(quality, "run_snyk", side_effect=lambda app, *a, **k: dict(self.snyk, app=app))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def go(self, skip=None, reason=None):
        return capture(lambda: dev_ticket.cmd_quality(self.api, mock.Mock(ticket="100", skip=skip, reason=reason)))[1]

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def test_pass_records_the_run_bound_to_the_files(self):
        res = self.go()
        self.assertEqual((res["status"], res["apps"]), ("PASS", ["DeviceMgmtAPI", "DeviceMgmtWebApp"]))
        self.assertEqual(len(res["sonar"]), 2)
        self.assertEqual([r["app"] for r in res["snyk"]], ["DeviceMgmtAPI"])  # only the app with a Snyk step
        self.assertEqual((self.state()["quality"]["status"], self.state()["quality"]["fingerprint"]), ("PASS", "fp1"))
        self.assertTrue((self.folder / "quality" / "quality-report.md").is_file())

    def test_blocking_sonar_or_fixable_snyk_fail(self):
        self.sonar = {"status": "FAIL", "blocking": [{"file": "f", "line": 1, "severity": "HIGH", "rule": "r", "message": "m"}], "low": []}
        res = self.go()
        self.assertEqual((res["status"], res["sonar"][0]["blocking"]), ("FAIL", 1))
        self.assertIn("Blocking (High / Medium)", (self.folder / "quality" / "quality-report.md").read_text(encoding="utf-8"))

    def test_a_tool_error_is_not_a_pass_but_can_be_skipped_with_a_reason(self):
        self.snyk = {"status": "NOT_AUTHENTICATED", "message": "auth"}
        self.assertEqual(self.go()["status"], "ERROR")
        self.assertEqual(self.go(skip=["snyk"])["status"], "REASON_REQUIRED")
        res = self.go(skip=["snyk"], reason="Snyk org access requested from DevOps")
        self.assertEqual((res["status"], res["skipped"]), ("PASS", {"snyk": "Snyk org access requested from DevOps"}))

    def test_no_token_is_reported(self):
        with mock.patch.dict("os.environ", {"SONAR_TOKEN": ""}):
            res = self.go()
        self.assertEqual((res["status"], res["sonar"][0]["status"]), ("ERROR", "NO_TOKEN"))

    def test_hand_off_guard(self):
        problem = quality_cmds.quality_problem
        with mock.patch.dict(dev_ticket.CONFIG, CFG):
            self.assertEqual(problem({}, "fp1")[0], "QUALITY_REQUIRED")
            self.assertEqual(problem({"quality": {"status": "PASS", "fingerprint": "old"}}, "fp1")[0], "QUALITY_STALE")
            self.assertEqual(problem({"quality": {"status": "FAIL", "fingerprint": "fp1"}}, "fp1")[0], "QUALITY_FAILED")
            self.assertIsNone(problem({"quality": {"status": "PASS", "fingerprint": "fp1"}}, "fp1"))
        with mock.patch.dict(dev_ticket.CONFIG, {"quality": {"enabled": False}}):
            self.assertIsNone(problem({}, "fp1"))

    def test_handoff_line_names_what_is_left(self):
        from develop_ticket.protocol import _quality_line, _quality_summary
        q = _quality_summary({"status": "PASS", "at": "t", "apps": ["DeviceMgmtWebApp"], "skipped": {"snyk": "no access"},
                              "sonar": [{"low_count": 2}], "snyk": []})
        line = _quality_line(q)
        self.assertIn("2 Low Sonar issue(s)", line)
        self.assertIn("snyk skipped: no access", line)


class FakeJenkins:
    """Code_Merge_Launchpad_CMGMT #20504 merged #100 at t=1000s (ran 600s); DeviceMgmtWebApp #1627 started inside it."""

    def __init__(self, app_console=SONAR_FAILED_CONSOLE):
        self.app_console = app_console

    def _get_json(self, path):
        if path.startswith("/job/Code_Merge_Launchpad_CMGMT/api"):
            return {"builds": [{"number": 20505, "timestamp": 2_000_000, "duration": 1000},
                               {"number": 20504, "timestamp": 1_000_000, "duration": 600_000}]}
        if path.startswith("/job/DeviceMgmtWebApp/api"):
            return {"builds": [{"number": 1628, "timestamp": 3_000_000, "result": "SUCCESS"},
                               {"number": 1627, "timestamp": 1_200_000, "result": "FAILURE"}]}
        raise AssertionError(path)

    def _request(self, path, *a, **k):
        if path == "/job/Code_Merge_Launchpad_CMGMT/20504/consoleText":
            return 200, "Creating merge request for ticket # 100\nBuild for DeviceMgmtWebApp is failed.\n"
        if path == "/job/Code_Merge_Launchpad_CMGMT/20505/consoleText":
            return 200, "Creating merge request for ticket # 999\n"
        if path == "/job/DeviceMgmtWebApp/1627/consoleText":
            return 200, self.app_console
        raise AssertionError(path)


HANDOFF = {"id": 5, "user_id": "u1", "created_on": "1970-01-01T00:00:10Z"}


class BounceTests(unittest.TestCase):
    def setUp(self):
        from develop_ticket.protocol import render_handoff
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "reviewer1"}
        self.api.get_active_tickets.return_value = [{"number": 100, "status": "Build Failed", "summary": "Fix"},
                                                    {"number": 5, "status": "Dev Queue"}]
        self.api.get_space_users.return_value = [{"id": "u1", "login": "dev1"}, {"id": "s1", "login": "reviewer1"}]
        self.api.get_user.side_effect = lambda uid: {"u1": {"login": "dev1"}, "s1": {"login": "reviewer1"}}.get(uid, {})
        payload = {"ticket": "100", "author": "dev1", "round": 1, "head": "a" * 40}
        with mock.patch("develop_ticket.protocol.json.dumps", side_effect=json.dumps):
            body = "x\nHANDOFF-JSON-BEGIN\n" + json.dumps(payload) + "\nHANDOFF-JSON-END\n"
        self.comments = [dict(HANDOFF, comment=body),
                         {"id": 6, "user_id": "bot", "created_on": "1970-01-01T00:20:00Z",
                          "comment": "Build for DeviceMgmtWebApp is failed."}]
        self.api.get_comments.side_effect = lambda n: list(self.comments) if str(n) == "100" else []
        self.jenkins = FakeJenkins()
        for patcher in (mock.patch.dict(dev_ticket.CONFIG, dict(CFG, reviewers=["reviewer1"])),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(quality_cmds, "_jenkins", side_effect=lambda: self.jenkins),
                        mock.patch.object(quality_cmds, "_sonar_issues", return_value=[
                            {"file": "src/x.html", "line": 337, "severity": "LOW", "rule": "Web:Mouse", "message": "Add a key handler"}])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def go(self, yes=False, confirm=None, ticket=None):
        return capture(lambda: dev_ticket.cmd_quality_bounce(self.api, mock.Mock(ticket=ticket, yes=yes, confirm=confirm)))[1]

    def test_preview_explains_why_and_changes_nothing(self):
        res = self.go()
        row = res["tickets"][0]
        self.assertEqual((res["status"], row["verdict"], row["reasons"], row["author"]), ("PREVIEW", "BOUNCE", ["SONAR"], "dev1"))
        self.assertEqual((row["merge"], row["apps"][0]["build"]), ({"job": "Code_Merge_Launchpad_CMGMT", "build": 20504}, 1627))
        self.assertEqual(row["apps"][0]["gate_conditions"][0]["metric"], "new_violations")
        self.api.add_comment.assert_not_called()
        self.api.assign_and_move.assert_not_called()

    def test_yes_needs_the_previewed_hash_then_comments_and_moves_to_the_author(self):
        token = self.go()["confirm"]
        self.assertEqual(self.go(yes=True)["status"], "CONFIRM_REQUIRED")
        res = self.go(yes=True, confirm=token)
        self.assertEqual(res["status"], "BOUNCED")
        body = self.api.add_comment.call_args.args[1]
        self.assertIn("new_violations = 2", body)
        self.assertIn("Add a key handler", body)
        self.assertIn("QUALITY-BOUNCE-JSON-BEGIN", body)
        self.api.assign_and_move.assert_called_once_with("100", "u1", "Dev Queue")

    def test_a_failure_for_another_reason_is_never_bounced(self):
        self.jenkins = FakeJenkins(COMPILE_FAILED_CONSOLE)
        res = self.go()
        self.assertEqual((res["status"], res["tickets"][0]["verdict"]), ("NOTHING_TO_BOUNCE", "NOT_QUALITY"))

    def test_already_bounced_is_skipped(self):
        self.comments.append({"id": 7, "user_id": "s1", "created_on": "1970-01-01T00:30:00Z",
                              "comment": quality_cmds.render_bounce({"ticket": "100", "author": "dev1", "to_status": "Dev Queue",
                                                                     "apps": [], "reasons": ["SONAR"]})})
        self.assertEqual(self.go()["tickets"][0]["verdict"], "ALREADY_BOUNCED")

    def test_only_reviewers_run_it_and_it_can_be_off(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.assertEqual(self.go()["status"], "NOT_A_REVIEWER")
        with mock.patch.dict(dev_ticket.CONFIG, {"quality": {"enabled": False}}):
            self.assertEqual(self.go()["status"], "BOUNCE_OFF")


class ReworkAfterBounceTests(trw.ReworkTests):
    """The author's `dt rework` treats a quality bounce like a REJECT verdict."""

    def setUp(self):
        super().setUp()
        bounce = quality_cmds.render_bounce({
            "ticket": "100", "author": "developer1", "to_status": "Dev Queue", "reasons": ["SONAR"], "round": 1, "head": trw.A,
            "apps": [{"app": "DeviceMgmtWebApp", "job": "DeviceMgmtWebApp", "build": 1627, "reasons": ["SONAR"],
                      "gate_conditions": [{"metric": "new_violations", "actual": "2", "threshold": "0", "comparator": "GT"}],
                      "sonar_issues": [{"file": "src/x.html", "line": 337, "severity": "LOW", "rule": "Web:M", "message": "key handler"}]}]})
        self.comments = [{"id": 20, "user_id": "s2", "created_on": "2026-10-06", "comment": bounce}]


for _name in [n for n in dir(trw.ReworkTests) if n.startswith("test")]:
    setattr(ReworkAfterBounceTests, _name, None)


def _bounce_rework_test(self):
    _code, res = self.go(yes=True)
    self.assertEqual((res["status"], res["reviewer"]), ("REWORK_READY", "reviewer2"))
    self.assertTrue(any("new_violations" in f["finding"] for f in res["findings"]))
    self.assertTrue(any("key handler" in f["finding"] for f in res["findings"]))
    state = self.state()
    self.assertEqual((state["stage"], state["round"]), ("6-build", 2))


def _older_bounce_loses_to_a_newer_verdict(self):
    self.comments.append(trw.verdict_comment(created="2026-10-07"))
    _code, res = self.go()
    self.assertEqual((res["status"], res["reviewer"]), ("PREVIEW", "reviewer1"))


ReworkAfterBounceTests.test_a_bounce_reopens_the_ticket_with_its_findings = _bounce_rework_test
ReworkAfterBounceTests.test_a_newer_review_verdict_wins = _older_bounce_loses_to_a_newer_verdict


if __name__ == "__main__":
    unittest.main()
