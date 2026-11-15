"""`dt profile`, `func-mode`, `func-prepare`, `func-record`, `env-check`, `env-apply`, `review-brief` and `review-check`
(improvements item 23). git, db_checks.py, review_check and the env/ports are mocked; everything is written under a temp dir."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dev_ticket
from develop_ticket import common

CFG = dev_ticket.CONFIG
ENV_REL = CFG["functional_checks"]["local_env_file"]
ENV_TS_REL = CFG["functional_checks"]["environment_ts"]
ANGULAR_ROOT = CFG["functional_checks"]["angular_roots"][0]

PLAN = """# Plan

## Approach
Something.

## Functional checks
- FC-1 open the grid
- FC-2 save a row

## Notes
trailing
"""

TEMPLATE = ("Ticket #{N} ({summary}) tier {tier}. Files: {files}. DB: {D} / {db_files}.\n"
            "Branch `FB_{D}` here. Keep {model} and {n}.\n")


def run(command, api=None, **kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100")
    vars(args).update(kwargs)
    with contextlib.redirect_stdout(out):
        code = command(api or mock.Mock(), args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / CFG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state_path = self.folder / "state.json"
        self.set_state({})
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(common, "SCRIPT_DIR", self.root / "python_scripts")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write(self, rel, text):
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def metrics(self):
        path = self.folder / "metrics.jsonl"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def no_state(self):
        self.state_path.unlink()


class ProfileTests(Base):
    def run_profile(self, files, force=None):
        with mock.patch("local_gates.changed_files", return_value=files):
            return run(dev_ticket.cmd_profile, force=force)

    def test_no_state_is_refused(self):
        self.no_state()
        self.assertEqual(self.run_profile([])[1]["status"], "NO_STATE")

    def test_angular_only_change_is_staging(self):
        code, res = self.run_profile([ANGULAR_ROOT + "src/a.ts", ANGULAR_ROOT + "src/a.html"])
        self.assertEqual((code, res["status"], res["profile"], res["files"], res["forced"]), (0, "PROFILE", "cloud_staging", 2, False))
        saved = self.state()["profile"]
        self.assertEqual((saved["name"], saved["forced"]), ("cloud_staging", False))

    def test_any_backend_file_makes_it_local(self):
        _, res = self.run_profile([ANGULAR_ROOT + "src/a.ts", "SomeService/Program.cs"])
        self.assertEqual(res["profile"], "local")
        self.assertEqual(self.state()["profile"]["name"], "local")

    def test_no_changed_files_is_none(self):
        _, res = self.run_profile([])
        self.assertEqual(res["profile"], "none")

    def test_force_overrides_the_decision_and_is_recorded(self):
        _, res = self.run_profile([ANGULAR_ROOT + "src/a.ts"], force="local")
        self.assertEqual((res["profile"], res["forced"]), ("local", True))
        self.assertTrue(self.state()["profile"]["forced"])
        self.assertEqual(self.state()["history"][-1]["result"], "profile=local")


class FuncModeTests(Base):
    def test_no_state_is_refused(self):
        self.no_state()
        code, res = run(dev_ticket.cmd_func_mode, mode="run", reason=None, confirmed=False)
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_skip_needs_a_reason(self):
        code, res = run(dev_ticket.cmd_func_mode, mode="skip", reason="  ", confirmed=True)
        self.assertEqual((code, res["status"]), (1, "REASON_REQUIRED"))
        self.assertNotIn("functional_mode", self.state())

    def test_skip_needs_the_developers_confirmation(self):
        code, res = run(dev_ticket.cmd_func_mode, mode="skip", reason="no UI", confirmed=False)
        self.assertEqual((code, res["status"]), (1, "CONFIRMATION_REQUIRED"))
        self.assertNotIn("functional_skip", self.state())

    def test_confirmed_skip_is_recorded_with_its_reason(self):
        code, res = run(dev_ticket.cmd_func_mode, mode="skip", reason=" no UI path ", confirmed=True)
        self.assertEqual((code, res["status"], res["mode"]), (0, "MODE_SET", "skip"))
        self.assertIn("skipped by the developer", res["note"])
        state = self.state()
        self.assertEqual((state["functional_mode"], state["functional_skip"]["reason"]), ("skip", "no UI path"))
        self.assertEqual([m["event"] for m in self.metrics()], ["functional_mode"])
        self.assertEqual(self.metrics()[0]["mode"], "skip")

    def test_run_clears_an_earlier_skip(self):
        self.set_state({"functional_mode": "skip", "functional_skip": {"reason": "x"}})
        code, res = run(dev_ticket.cmd_func_mode, mode="run", reason=None, confirmed=False)
        self.assertEqual((code, res["mode"], res["note"]), (0, "run", None))
        state = self.state()
        self.assertEqual(state["functional_mode"], "run")
        self.assertNotIn("functional_skip", state)

    def test_later_is_accepted_without_reason(self):
        _, res = run(dev_ticket.cmd_func_mode, mode="later", reason=None, confirmed=False)
        self.assertEqual(res["status"], "MODE_SET")
        self.assertEqual(self.state()["functional_mode"], "later")


class FuncPrepareTests(Base):
    def test_no_state_is_refused(self):
        self.no_state()
        self.assertEqual(run(dev_ticket.cmd_func_prepare)[1]["status"], "NO_STATE")

    def test_plan_missing_or_without_section_is_refused(self):
        code, res = run(dev_ticket.cmd_func_prepare)
        self.assertEqual((code, res["status"]), (1, "NO_PLANNED_CHECKS"))
        self.write("PLAN.md", "# Plan\n\n## Approach\nx\n")
        self.assertEqual(run(dev_ticket.cmd_func_prepare)[1]["status"], "NO_PLANNED_CHECKS")
        self.assertFalse((self.folder / "FUNCTIONAL_CHECKS.md").exists())

    def test_brief_is_written_from_the_plan_section(self):
        self.write("PLAN.md", PLAN)
        self.set_state({"profile": {"name": "cloud_staging"}})
        code, res = run(dev_ticket.cmd_func_prepare)
        self.assertEqual((code, res["status"]), (0, "PREPARED"))
        brief = (self.folder / "FUNCTIONAL_CHECKS.md").read_text(encoding="utf-8")
        self.assertEqual(res["brief"], str(self.folder / "FUNCTIONAL_CHECKS.md"))
        self.assertIn("# Functional checks — #100", brief)
        self.assertIn("## Checks\n- FC-1 open the grid\n- FC-2 save a row", brief)
        self.assertNotIn("trailing", brief)
        self.assertIn("the staging APIs", brief)
        out = str(self.folder / "reviews" / "functional-checks-rerun.md")
        self.assertEqual(res["results_file"], out)
        self.assertIn(f"dt func-record 100 --from {out}", brief)

    def test_without_a_profile_the_brief_targets_local(self):
        self.write("PLAN.md", PLAN)
        run(dev_ticket.cmd_func_prepare)
        brief = (self.folder / "FUNCTIONAL_CHECKS.md").read_text(encoding="utf-8")
        self.assertIn("local services with the test DB", brief)


class FuncRecordTests(Base):
    FP = "fingerprint-abc"

    def record(self, **kwargs):
        kwargs.setdefault("from_file", None)
        kwargs.setdefault("passed", 0)
        kwargs.setdefault("failed", 0)
        with mock.patch("local_gates.changed_files", return_value=["a.cs"]), \
                mock.patch("local_gates.diff_fingerprint", return_value=self.FP):
            return run(dev_ticket.cmd_func_record, **kwargs)

    def test_no_state_is_refused(self):
        self.no_state()
        self.assertEqual(self.record()[1]["status"], "NO_STATE")

    def test_no_planned_checks_is_refused(self):
        code, res = self.record(passed=1)
        self.assertEqual((code, res["status"]), (1, "NO_PLANNED_CHECKS"))
        self.assertNotIn("functional", self.state())

    def test_manual_counts_must_equal_the_planned_checks(self):
        self.write("PLAN.md", PLAN)
        code, res = self.record(passed=1, failed=0)
        self.assertEqual((code, res["status"], res["planned"], res["reported"]), (1, "COUNT_MISMATCH", 2, 1))
        self.assertNotIn("functional", self.state())

    def test_all_passed_is_recorded_and_selects_run_mode(self):
        self.write("PLAN.md", PLAN)
        self.set_state({"functional_mode": "skip", "functional_skip": {"reason": "x"}, "profile": {"name": "local"}})
        code, res = self.record(passed=2, failed=0)
        self.assertEqual((code, res["status"], res["passed"], res["planned"], res["profile"]), (0, "PASS", 2, 2, "local"))
        state = self.state()
        self.assertEqual((state["functional"]["status"], state["functional"]["fingerprint"]), ("PASS", self.FP))
        self.assertEqual(state["functional_mode"], "run")
        self.assertNotIn("functional_skip", state)
        self.assertEqual(state["history"][-1]["result"], "functional PASS")

    def test_a_failure_is_recorded_and_exits_1(self):
        self.write("PLAN.md", PLAN)
        code, res = self.record(passed=1, failed=1)
        self.assertEqual((code, res["status"], res["failed"]), (1, "FAIL", 1))
        self.assertEqual(self.state()["functional"]["status"], "FAIL")

    def test_from_file_outside_the_ticket_folder_is_refused(self):
        self.write("PLAN.md", PLAN)
        outside = self.root / "results.md"
        outside.write_text("FC-1 FC-2\npassed 2 failed 0\n", encoding="utf-8")
        code, res = self.record(from_file=str(outside))
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))

    def test_from_missing_file_is_refused(self):
        self.write("PLAN.md", PLAN)
        code, res = self.record(from_file=str(self.folder / "reviews" / "nope.md"))
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))

    def test_from_file_without_result_line_is_refused(self):
        self.write("PLAN.md", PLAN)
        res_file = self.write("reviews/r.md", "| FC-1 | PASS |\n| FC-2 | PASS |\n")
        code, res = self.record(from_file=str(res_file))
        self.assertEqual((code, res["status"]), (1, "NO_RESULT_LINE"))

    def test_from_file_missing_a_check_id_is_refused(self):
        self.write("PLAN.md", PLAN)
        res_file = self.write("reviews/r.md", "| FC-1 | PASS |\npassed 2 failed 0\n")
        code, res = self.record(from_file=str(res_file))
        self.assertEqual((code, res["status"], res["missing"]), (1, "MISSING_CHECK_IDS", ["FC-2"]))

    def test_from_file_totals_are_taken_and_evidence_kept(self):
        self.write("PLAN.md", PLAN)
        res_file = self.write("reviews/r.md", "| FC-1 | PASS |\n| FC-2 | FAIL |\n\npassed 1 failed 1\n")
        code, res = self.record(from_file=str(res_file))
        self.assertEqual((code, res["status"], res["passed"], res["failed"]), (1, "FAIL", 1, 1))
        self.assertEqual(self.state()["functional"]["evidence"], str(res_file))

    def test_from_file_totals_must_match_the_plan(self):
        self.write("PLAN.md", PLAN)
        res_file = self.write("reviews/r.md", "FC-1 FC-2\npassed 1 failed 0\n")
        code, res = self.record(from_file=str(res_file))
        self.assertEqual((code, res["status"]), (1, "COUNT_MISMATCH"))


class EnvTests(Base):
    def write_env(self, text):
        path = self.root / ENV_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_profile_none_needs_no_env(self):
        code, res = run(dev_ticket.cmd_env_check, profile="none")
        self.assertEqual((code, res["status"]), (0, "ENV_READY"))

    def test_missing_env_file_is_reported(self):
        code, res = run(dev_ticket.cmd_env_check, profile="cloud_staging")
        self.assertEqual((code, res["status"], res["file"]), (1, "ENV_FILE_MISSING", ENV_REL))

    def test_local_profile_reports_missing_keys_by_name_only(self):
        self.write_env("cloud_staging.GatewayAPIUrl=http://stg\n")
        code, res = run(dev_ticket.cmd_env_check, profile="local")
        self.assertEqual((code, res["status"]), (1, "ENV_PROBLEMS"))
        self.assertIn("missing local.GatewayAPIUrl", res["problems"])
        self.assertNotIn("http://stg", json.dumps(res))

    def test_staging_profile_ready_lists_keys_not_values(self):
        self.write_env("cloud_staging.GatewayAPIUrl=http://secret-host\ncloud_staging.OtherUrl=http://o\nservice.api.cmd=run\n")
        code, res = run(dev_ticket.cmd_env_check, profile="cloud_staging")
        self.assertEqual((code, res["status"]), (0, "ENV_READY"))
        self.assertEqual(res["url_keys"], ["GatewayAPIUrl", "OtherUrl"])
        self.assertEqual((res["local_serving"], res["services"]), ({}, ["api"]))
        self.assertNotIn("secret-host", json.dumps(res))

    def test_profile_comes_from_state_when_not_given(self):
        self.set_state({"profile": {"name": "none"}})
        self.assertEqual(run(dev_ticket.cmd_env_check, profile=None)[1]["profile"], "none")

    def test_profile_names_in_older_state_files_still_work(self):
        self.write_env("cloud_staging.GatewayAPIUrl=http://stg\n")
        self.set_state({"profile": {"name": "staging"}})  # written before the cloud_ rename
        code, res = run(dev_ticket.cmd_env_check, profile=None)
        self.assertEqual((code, res["status"], res["profile"]), (0, "ENV_READY", "cloud_staging"))

    def test_env_apply_with_no_urls_does_nothing(self):
        self.write_env("local.db.connection=x\n")
        code, res = run(dev_ticket.cmd_env_apply, profile="cloud_staging")
        self.assertEqual((code, res["status"]), (0, "NOTHING_TO_APPLY"))

    def test_env_apply_rewrites_only_matching_keys(self):
        self.write_env("cloud_staging.GatewayAPIUrl=http://stg\ncloud_staging.Missing=http://m\ncloud_staging.db.x=skip\n")
        ts = self.root / ENV_TS_REL
        ts.parent.mkdir(parents=True, exist_ok=True)
        ts.write_text("export const environment = {\n  GatewayAPIUrl: 'http://old',\n  // Missing: 'x',\n};\n", encoding="utf-8")
        code, res = run(dev_ticket.cmd_env_apply, profile="cloud_staging")
        self.assertEqual((code, res["status"], res["changed_keys"], res["unchanged_keys"]),
                         (0, "APPLIED", ["GatewayAPIUrl"], ["Missing"]))
        text = ts.read_text(encoding="utf-8")
        self.assertIn("GatewayAPIUrl: 'http://stg'", text)
        self.assertIn("// Missing: 'x'", text)


class ProfileChoiceTests(Base):
    """Preprod: an Angular-only change asks staging vs preprod; anything else stays local."""

    def run_profile(self, files, force=None, choose=None):
        with mock.patch("local_gates.changed_files", return_value=files):
            return run(dev_ticket.cmd_profile, force=force, choose=choose)

    def test_angular_only_offers_the_choice(self):
        _, res = self.run_profile([ANGULAR_ROOT + "src/a.ts"])
        self.assertEqual((res["profile"], res["needs_choice"], res["choose_between"]),
                         ("cloud_staging", True, ["cloud_staging", "cloud_preprod"]))

    def test_choosing_preprod_is_recorded(self):
        _, res = self.run_profile([ANGULAR_ROOT + "src/a.ts"], choose="cloud_preprod")
        self.assertEqual((res["profile"], res["needs_choice"], res["forced"]), ("cloud_preprod", False, False))
        self.assertEqual(self.state()["profile"]["name"], "cloud_preprod")

    def test_backend_change_cannot_choose_and_stays_local(self):
        code, res = self.run_profile([ANGULAR_ROOT + "src/a.ts", "S/Program.cs"], choose="cloud_preprod")
        self.assertEqual((code, res["status"], res["decided"]), (1, "CHOICE_NOT_ALLOWED", "local"))
        self.assertEqual(self.run_profile(["S/Program.cs"])[1]["needs_choice"], False)

    def test_force_preprod(self):
        _, res = self.run_profile([ANGULAR_ROOT + "src/a.ts"], force="cloud_preprod")
        self.assertEqual((res["profile"], res["forced"], res["needs_choice"]), ("cloud_preprod", True, False))



class EnvOverlayTests(Base):
    """env-apply / env-restore over environment.ts of every app and the git-ignored appsettings overlays."""
    SVC = "wfapi_wf_service/wfapi_wf_microservice"

    def setUp(self):
        super().setUp()
        # three levels deep, so the configured host repo path (../../../C2M_CommonServices/...) resolves inside the temp root
        script_dir = mock.patch.object(common, "SCRIPT_DIR", self.root / "x" / "y" / "python_scripts")
        script_dir.start()
        self.addCleanup(script_dir.stop)
        patcher = mock.patch.object(common, "git", return_value=SimpleNamespace(returncode=0, stdout=""))
        self.git = patcher.start()
        self.addCleanup(patcher.stop)
        self.put(ENV_REL, "local.GatewayAPIUrl=http://localhost:5000\nlocal.db.allowed_host=testhost\n"
                          "local.db.allowed_name=c2m_test\ncloud_staging.GatewayAPIUrl=https://stg.net\n"
                          "cloud_preprod.GatewayAPIUrl=https://pre.net\n"
                          f"service.wf.cwd={self.SVC}\nservice.wf.cmd=run\nservice.wf.url=http://localhost:1\n")
        self.put(self.SVC + "/appsettings.json",
                 '{"ConnectionStrings": {"Connection": "Server=orig;Database=orig"}, "AppSettings": {"Url": "u0"}}')
        self.put(ENV_TS_REL, "export const environment = {\n  GatewayAPIUrl: 'http://old',\n};\n")
        self.put("C2MWFAng/src/environments/environment.ts", "export const environment = {\n  GatewayAPIUrl: 'http://old2',\n};\n")

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def overlay(self, profile, data):
        self.put(f"{self.SVC}/appsettings.{profile}.json", json.dumps(data))

    def test_preprod_urls_go_into_every_apps_environment_ts(self):
        code, res = run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        self.assertEqual((code, res["status"], res["changed_keys"]), (0, "APPLIED", ["GatewayAPIUrl"]))
        self.assertIn("'https://pre.net'", self.read(ENV_TS_REL))
        self.assertIn("'https://pre.net'", self.read("C2MWFAng/src/environments/environment.ts"))
        self.assertEqual(len(self.state()["env_patched"]), 2)

    def test_host_application_environment_ts_is_patched_and_restored(self):
        host = (self.root / "x" / "y" / "python_scripts" / CFG["ui_checks"]["host_repo_path"]).resolve()
        self.assertTrue(host.is_relative_to(self.root.resolve()))
        ts = host / "src" / "environments" / "environment.ts"
        ts.parent.mkdir(parents=True)
        ts.write_text('export const environment = {\n  Setting: {\n    GatewayAPIUrl:\'http://old\',\n'
                      '    WF_Api_Url : "http://oldwf",\n  }\n};\n', encoding="utf-8")
        _, res = run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        self.assertIn("host:src/environments/environment.ts", res["files"])
        text = ts.read_text(encoding="utf-8")
        self.assertIn("GatewayAPIUrl:'https://pre.net'", text)
        self.assertIn('WF_Api_Url : "http://oldwf"', text)  # no cloud_preprod.WF_Api_Url key: left alone
        self.assertEqual(len(self.state()["env_patched"]), 3)
        self.assertEqual(run(dev_ticket.cmd_env_restore)[1]["status"], "RESTORED")
        self.assertIn("GatewayAPIUrl:'http://old'", ts.read_text(encoding="utf-8"))

    def test_restore_returns_the_originals_and_clears_the_guard(self):
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        code, res = run(dev_ticket.cmd_env_restore)
        self.assertEqual((code, res["status"], len(res["restored"])), (0, "RESTORED", 2))
        self.assertIn("'http://old'", self.read(ENV_TS_REL))
        self.assertNotIn("env_patched", self.state())
        self.assertEqual(run(dev_ticket.cmd_env_restore)[1]["status"], "NOTHING_TO_RESTORE")

    def test_second_apply_keeps_the_first_snapshot(self):
        run(dev_ticket.cmd_env_apply, profile="cloud_staging")
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        run(dev_ticket.cmd_env_restore)
        self.assertIn("'http://old'", self.read(ENV_TS_REL))

    def test_local_overlay_patches_appsettings_incl_connection_string_and_hides_it_from_git(self):
        self.overlay("local", {"ConnectionStrings": {"Connection": "Server=testhost;Database=c2m_test"},
                               "AppSettings": {"Url": "u1", "Typo": 1}})
        code, res = run(dev_ticket.cmd_env_apply, profile="local")
        self.assertEqual((code, res["status"]), (0, "APPLIED"))
        info = res["appsettings"]["wf"]
        self.assertEqual((info["changed"], info["missing_keys"]), (["ConnectionStrings:Connection", "AppSettings:Url"], ["AppSettings:Typo"]))
        self.assertEqual(json.loads(self.read(self.SVC + "/appsettings.json"))["AppSettings"]["Url"], "u1")
        self.assertIn(mock.call("update-index", "--skip-worktree", self.SVC + "/appsettings.json", check=False),
                      self.git.call_args_list)
        self.assertNotIn("testhost", json.dumps(res))
        run(dev_ticket.cmd_env_restore)
        self.assertEqual(json.loads(self.read(self.SVC + "/appsettings.json"))["AppSettings"]["Url"], "u0")
        self.assertIn(mock.call("update-index", "--no-skip-worktree", self.SVC + "/appsettings.json", check=False),
                      self.git.call_args_list)

    def test_connection_string_to_another_db_is_refused_before_anything_is_written(self):
        self.overlay("local", {"ConnectionStrings": {"Connection": "Server=prod;Database=C2M"}})
        code, res = run(dev_ticket.cmd_env_apply, profile="local")
        self.assertEqual((code, res["status"]), (1, "ENV_PROBLEMS"))
        self.assertIn("'http://old'", self.read(ENV_TS_REL))
        self.assertNotIn("env_patched", self.state())

    def test_remote_profiles_never_read_an_appsettings_file_as_overlay(self):
        # appsettings.cloud_preprod.json in a service folder is that environment's own deploy file
        self.overlay("cloud_preprod", {"ConnectionStrings": {"Connection": "Server=prod;Database=C2M"}})
        code, res = run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        self.assertEqual((code, res["status"], res["appsettings"]), (0, "APPLIED", {}))
        self.assertEqual(json.loads(self.read(self.SVC + "/appsettings.json"))["ConnectionStrings"]["Connection"],
                         "Server=orig;Database=orig")
        self.assertEqual(run(dev_ticket.cmd_env_check, profile="cloud_preprod")[1]["appsettings_overlays"], [])

    def test_appsettings_with_own_uncommitted_changes_is_refused(self):
        self.overlay("local", {"AppSettings": {"Url": "u1"}})
        self.git.return_value = SimpleNamespace(returncode=0, stdout=" M appsettings.json\n")
        code, res = run(dev_ticket.cmd_env_apply, profile="local")
        self.assertEqual((code, res["status"]), (1, "APPSETTINGS_DIRTY"))
        self.assertEqual(json.loads(self.read(self.SVC + "/appsettings.json"))["AppSettings"]["Url"], "u0")

    def record(self, **kwargs):
        with mock.patch("local_gates.changed_files", return_value=["a.cs"]), \
                mock.patch("local_gates.diff_fingerprint", return_value="fp"):
            return run(dev_ticket.cmd_func_record, from_file=None, **kwargs)

    def test_func_record_puts_the_patched_files_back_on_pass(self):
        self.write("PLAN.md", PLAN)
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        code, res = self.record(passed=2, failed=0)
        self.assertEqual((code, len(res["env_restored"])), (0, 2))
        self.assertIn("'http://old'", self.read(ENV_TS_REL))
        self.assertNotIn("env_patched", self.state())

    def test_func_record_keeps_the_patch_on_fail_for_the_next_round(self):
        self.write("PLAN.md", PLAN)
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        code, res = self.record(passed=1, failed=1)
        self.assertEqual((code, res["status"]), (1, "FAIL"))
        self.assertNotIn("env_restored", res)
        self.assertIn("'https://pre.net'", self.read(ENV_TS_REL))
        self.assertEqual(len(self.state()["env_patched"]), 2)

    def test_func_record_without_patched_files_reports_no_restore(self):
        self.write("PLAN.md", PLAN)
        self.assertNotIn("env_restored", self.record(passed=2, failed=0)[1])

    def test_auto_restore_puts_files_back_and_clears_the_guard_before_a_gate(self):
        from develop_ticket.functional_cmds import auto_restore_env
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        state = self.state()
        self.assertEqual(len(auto_restore_env("100", state)), 2)
        self.assertNotIn("env_patched", state)
        self.assertNotIn("env_patched", self.state())
        self.assertIn("'http://old'", self.read(ENV_TS_REL))
        self.assertEqual(auto_restore_env("100", state), [])

    def test_auto_restore_without_a_snapshot_keeps_the_guard(self):
        import functional
        from develop_ticket.functional_cmds import auto_restore_env
        run(dev_ticket.cmd_env_apply, profile="cloud_preprod")
        next((self.folder / "env-backup").iterdir()).unlink()
        state = self.state()
        self.assertEqual(len(auto_restore_env("100", state)), 1)
        self.assertEqual(len(state["env_patched"]), 1)
        self.assertEqual(functional.gate_problem(state, lambda: "fp")[0], "ENV_PATCHED")

    def test_bad_url_and_bad_overlay_json_are_reported(self):
        self.put(ENV_REL, "cloud_preprod.GatewayAPIUrl=not-a-url\n")
        self.assertEqual(run(dev_ticket.cmd_env_apply, profile="cloud_preprod")[1]["status"], "ENV_PROBLEMS")
        self.put(ENV_REL, f"local.GatewayAPIUrl=http://localhost:5000\nservice.wf.cwd={self.SVC}\n")
        self.put(f"{self.SVC}/appsettings.local.json", "{oops")
        self.assertEqual(run(dev_ticket.cmd_env_apply, profile="local")[1]["status"], "OVERLAY_INVALID")


class ReviewBriefTests(Base):
    def setUp(self):
        super().setUp()
        tpl = self.root / "skills" / "dev-ticket" / "REVIEW.md"
        tpl.parent.mkdir(parents=True)
        tpl.write_text(TEMPLATE, encoding="utf-8")
        self.tpl = tpl
        self.write("ticket.md", "# Ticket 100 — Fix the grid\n\nBody\n")
        self.set_state({"risk": "MEDIUM", "gates": {"files": ["a.cs", "b.ts"]}})
        patcher = mock.patch("subprocess.run")
        self.sub = patcher.start()
        self.addCleanup(patcher.stop)

    def brief(self, **kwargs):
        kwargs.setdefault("reviewer", False)
        kwargs.setdefault("round", 1)
        return run(dev_ticket.cmd_review_brief, **kwargs)

    def test_no_state_is_refused(self):
        self.no_state()
        self.assertEqual(self.brief()[1]["status"], "NO_STATE")

    def test_missing_inputs_are_listed(self):
        self.set_state({})
        code, res = self.brief()
        self.assertEqual((code, res["status"]), (1, "BRIEF_INPUTS_MISSING"))
        self.assertEqual(res["missing"], ["tier", "gates.files"])
        self.assertTrue(res["template_found"])

    def test_missing_ticket_md_and_template(self):
        (self.folder / "ticket.md").unlink()
        self.tpl.unlink()
        _, res = self.brief()
        self.assertEqual((res["status"], res["missing"], res["template_found"]),
                         ("BRIEF_INPUTS_MISSING", ["summary (ticket.md)"], False))

    def test_values_are_substituted_and_reviewer_tokens_left(self):
        code, res = self.brief()
        self.assertEqual((code, res["status"], res["tier"], res["files"], res["db_files"], res["reviewer"]),
                         (0, "BRIEF_WRITTEN", "MEDIUM", 2, 0, False))
        out = self.folder / "reviews" / "REVIEW_BRIEF.md"
        self.assertEqual(res["brief"], str(out))
        self.assertEqual(res["review_file"], str(self.folder / "reviews" / "code-review.md"))
        text = out.read_text(encoding="utf-8")
        self.assertIn("Ticket #100 (Fix the grid) tier MEDIUM. Files: a.cs, b.ts. DB: none / none.", text)
        self.assertIn("no DB ticket here", text)
        self.assertNotIn("FB_", text)
        self.assertIn("{model} and {n}", text)
        self.assertNotIn("Inputs override", text)
        self.sub.assert_not_called()

    def test_round_two_uses_its_own_file_names(self):
        _, res = self.brief(round=2)
        self.assertTrue(res["brief"].endswith("REVIEW_BRIEF-round2.md"))
        self.assertTrue(res["review_file"].endswith("code-review-round2.md"))
        self.assertTrue((self.folder / "reviews" / "REVIEW_BRIEF-round2.md").is_file())
        self.assertFalse((self.folder / "reviews" / "REVIEW_BRIEF.md").exists())

    def test_tier_falls_back_to_risk_check_min_tier(self):
        self.set_state({"risk_check": {"min_tier": "HIGH"}, "gates": {"files": ["a.cs"]}})
        self.assertEqual(self.brief()[1]["tier"], "HIGH")

    def test_reviewer_uses_review_tier_and_appends_override(self):
        self.set_state({"risk": "LOW", "review": {"tier": "HIGH"}, "gates": {"files": ["a.cs"]}})
        _, res = self.brief(reviewer=True, round=3)
        self.assertEqual((res["tier"], res["reviewer"]), ("HIGH", True))
        text = (self.folder / "reviews" / "REVIEW_BRIEF-round3.md").read_text(encoding="utf-8")
        self.assertIn("tier HIGH", text)
        self.assertIn("## Inputs override", text)
        self.assertIn("DEV_TICKETS\\100\\ticket.md", text)
        self.assertIn("Write only `reviews\\code-review-round3.md`", text)
        self.assertNotIn("{N}", text.split("## Inputs override")[1])

    def test_non_reviewer_ignores_the_review_tier(self):
        self.set_state({"risk": "LOW", "review": {"tier": "HIGH"}, "gates": {"files": ["a.cs"]}})
        self.assertEqual(self.brief()[1]["tier"], "LOW")

    def test_db_files_add_db_ticket_and_db_checks_output(self):
        self.set_state({"risk": "LOW", "gates": {"files": ["a.cs"]}, "db": {"ticket": "200", "files": ["x.sql", "y.sql"]}})
        self.sub.return_value = SimpleNamespace(returncode=1, stdout="info\n  STOP bad thing\nWARN careful\nok\n", stderr="")
        _, res = self.brief()
        self.assertEqual(res["db_files"], 2)
        text = (self.folder / "reviews" / "REVIEW_BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("DB: 200 / x.sql, y.sql.", text)
        self.assertIn("`FB_200`", text)
        self.assertIn("## DB-checks output (`db_checks.py --ticket 100`)", text)
        self.assertIn("- STOP bad thing\n- WARN careful", text)
        self.assertNotIn("info", text.split("DB-checks output")[1])
        self.assertEqual(self.sub.call_args[0][0][-2:], ["--ticket", "100"])

    def test_db_checks_with_no_findings_says_none(self):
        self.set_state({"risk": "LOW", "gates": {"files": ["a.cs"]}, "db": {"files": ["x.sql"]}})
        self.sub.return_value = SimpleNamespace(returncode=0, stdout="all fine\n", stderr="")
        self.brief()
        text = (self.folder / "reviews" / "REVIEW_BRIEF.md").read_text(encoding="utf-8")
        self.assertTrue(text.rstrip().endswith("\nnone"))

    def test_db_checks_crash_is_reported_in_the_brief(self):
        self.set_state({"risk": "LOW", "gates": {"files": ["a.cs"]}, "db": {"files": ["x.sql"]}})
        self.sub.return_value = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        self.brief()
        text = (self.folder / "reviews" / "REVIEW_BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("db_checks could not run (exit 2)", text)


class ReviewCheckTests(Base):
    def check(self, outcome=None, **kwargs):
        kwargs.setdefault("file", None)
        with mock.patch("review_check.check_review", return_value=outcome) as chk:
            code, res = run(dev_ticket.cmd_review_check, **kwargs)
        return code, res, chk

    def test_missing_file_is_refused(self):
        code, res, chk = self.check()
        self.assertEqual((code, res["status"]), (1, "NO_FILE"))
        chk.assert_not_called()

    def test_no_findings_table_is_refused(self):
        self.write("reviews/code-review.md", "# review\n")
        outcome = {"findings": 0, "checked": 0, "ok": 0, "unverified": [], "not_checkable": []}
        code, res, _ = self.check(outcome)
        self.assertEqual((code, res["status"]), (1, "NO_FINDINGS_TABLE"))

    def test_all_verified_is_ok(self):
        path = self.write("reviews/code-review.md", "# review\n")
        outcome = {"findings": 2, "checked": 2, "ok": 2, "unverified": [], "not_checkable": []}
        code, res, chk = self.check(outcome)
        self.assertEqual((code, res["status"], res["ok"], res["unverified"]), (0, "REVIEW_CHECK_OK", 2, []))
        self.assertEqual(chk.call_args[0][0], path)
        self.assertEqual(chk.call_args[0][1], self.root)

    def test_unverified_findings_exit_2_and_are_listed(self):
        self.write("reviews/code-review.md", "# review\n")
        outcome = {"findings": 2, "checked": 2, "ok": 1, "unverified": ["F1"], "not_checkable": []}
        code, res, _ = self.check(outcome)
        self.assertEqual((code, res["status"], res["unverified"]), (2, "REVIEW_CHECK_UNVERIFIED", ["F1"]))
        self.assertIn("1 finding(s) could not be verified", res["message"])

    def test_relative_file_resolves_inside_the_ticket_folder(self):
        path = self.write("reviews/code-review-round2.md", "# review\n")
        outcome = {"findings": 1, "checked": 1, "ok": 1, "unverified": [], "not_checkable": []}
        with mock.patch("review_check.check_review", return_value=outcome) as chk:
            run(dev_ticket.cmd_review_check, file=str(Path("reviews") / "code-review-round2.md"))
        self.assertEqual(chk.call_args[0][0], path)


if __name__ == "__main__":
    unittest.main()
