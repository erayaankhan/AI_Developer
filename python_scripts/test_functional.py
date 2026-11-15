"""Guards of the functional checks: profile choice, test-DB allow-list, plan check ids, environment.ts edit."""
import tempfile
import unittest
from pathlib import Path

import functional

ROOTS = ["c2mwf_admin/", "c2mwf_runtime/", "C2MWFAng/"]


class ProfileTests(unittest.TestCase):
    def test_angular_only_is_staging(self):
        self.assertEqual(functional.profile_for(["c2mwf_admin/src/app/a.ts", "C2MWFAng/b.html"], ROOTS), "cloud_staging")

    def test_backend_or_mixed_is_local(self):
        self.assertEqual(functional.profile_for(["wfapi_wf_service/x.cs"], ROOTS), "local")
        self.assertEqual(functional.profile_for(["c2mwf_admin/a.ts", "wfapi_wf_service/x.cs"], ROOTS), "local")
        self.assertEqual(functional.profile_for(["c2mwf_admin/Api.csproj"], ROOTS), "local")

    def test_no_files_is_none(self):
        self.assertEqual(functional.profile_for([], ROOTS), "none")


class EnvTests(unittest.TestCase):
    def good(self):
        return {"local.db.connection": "Server=TestHost;Database=C2M_Test;Uid=u;Pwd=p",
                "local.db.allowed_host": "testhost", "local.db.allowed_name": "c2m_test",
                "local.GatewayAPIUrl": "http://localhost:5000"}

    def test_matching_test_db_is_ok(self):
        self.assertEqual(functional.env_problems("local", self.good(), ["GatewayAPIUrl"]), [])

    def test_other_db_is_refused(self):
        env = self.good()
        env["local.db.connection"] = "Server=prod-host;Database=C2M;Uid=u;Pwd=p"
        self.assertTrue(functional.env_problems("local", env))

    def test_missing_keys_are_reported_by_name_only(self):
        problems = functional.env_problems("local", {})
        self.assertIn("missing local.db.connection", problems)
        self.assertNotIn("Pwd", " ".join(problems))

    def test_profile_urls_skip_db_and_env_keys(self):
        self.assertEqual(functional.profile_urls("local", self.good()), {"GatewayAPIUrl": "http://localhost:5000"})


class PlanAndFileTests(unittest.TestCase):
    def test_check_ids_from_section(self):
        plan = "# P\n## Functional checks\n- [ ] FC-1 (AC 1): x\n- [ ] FC-2: y\n## Other\n- FC-9\n"
        self.assertEqual(functional.plan_check_ids(plan), ["FC-1", "FC-2"])
        self.assertEqual(functional.plan_check_ids("## UI checks\n- UI-1: a\n"), ["UI-1"])
        self.assertEqual(functional.plan_check_ids("# nothing"), [])

    def test_a_letter_suffix_is_its_own_check(self):
        plan = "## Functional checks\n- [ ] FC-5: a\n- [ ] FC-5b: b\n- [ ] FC-6: c\n"
        self.assertEqual(functional.plan_check_ids(plan), ["FC-5", "FC-5b", "FC-6"])

    def test_desktop_hand_off(self):
        plan = "# P\n## Functional checks\n- [ ] FC-1: x\n- [ ] FC-2: y\n## Other\n- FC-9\n"
        section = functional.plan_checks_section(plan)
        self.assertEqual(section, "- [ ] FC-1: x\n- [ ] FC-2: y")
        brief = functional.desktop_brief("1", "cloud_staging", section, "C:\\t\\out.md")
        self.assertIn("FC-2: y", brief)
        self.assertIn("C:\\t\\out.md", brief)
        self.assertNotIn("FC-9", brief)
        self.assertEqual(functional.parse_result_line("| FC-1 | PASS |\n\npassed 2 failed 0\n"), (2, 0))
        self.assertEqual(functional.parse_result_line("passed 1 failed 1\nlater\n**passed 3 failed 0**"), (3, 0))
        self.assertIsNone(functional.parse_result_line("| FC-1 | PASS |"))

    def test_environment_ts_edit_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "environment.ts"
            path.write_text("  //GatewayAPIUrl:'old',\n  GatewayAPIUrl: 'https://stg',\n", encoding="utf-8")
            self.assertEqual(functional.apply_environment_ts(path, {"GatewayAPIUrl": "http://localhost:5000"}),
                             ["GatewayAPIUrl"])
            text = path.read_text(encoding="utf-8")
            self.assertIn("//GatewayAPIUrl:'old'", text)
            self.assertIn("GatewayAPIUrl: 'http://localhost:5000'", text)


class OverlayTests(unittest.TestCase):
    ENV = {"local.db.allowed_host": "TestHost, other", "local.db.allowed_name": "c2m_test,c2m_log"}

    def test_merge_sets_existing_keys_only_and_reports_the_rest(self):
        data = {"ConnectionStrings": {"Connection": "old"}, "AppSettings": {"Url": "u1", "Same": 1}}
        changed, missing = functional.merge_overlay(
            data, {"connectionstrings": {"CONNECTION": "new"}, "AppSettings": {"Url": "u2", "Same": 1, "Nope": 2}})
        self.assertEqual(changed, ["connectionstrings:CONNECTION", "AppSettings:Url"])
        self.assertEqual(missing, ["AppSettings:Nope"])
        self.assertEqual((data["ConnectionStrings"]["Connection"], data["AppSettings"]["Url"]), ("new", "u2"))

    def test_apply_overlay_rewrites_json_and_rejects_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "appsettings.json"
            path.write_text('{"A": {"B": "x"}}', encoding="utf-8")
            self.assertEqual(functional.apply_overlay(path, {"A": {"B": "y"}}), (["A:B"], []))
            self.assertIn('"B": "y"', path.read_text(encoding="utf-8"))
            path.write_text('{"A": 1} // note', encoding="utf-8")
            with self.assertRaises(ValueError):
                functional.apply_overlay(path, {"A": 2})

    def test_connection_strings_must_match_the_allow_list(self):
        good = {"ConnectionStrings": {"Connection": "Server=testhost;Database=C2M_Test;Pwd=p",
                                      "LogDB": "Server=other;Database=c2m_log;Pwd=p"}}
        self.assertEqual(functional.overlay_db_problems("local", good, self.ENV), [])
        bad = {"ConnectionStrings": {"Connection": "Server=prod;Database=C2M;Pwd=SECRET"}}
        problems = functional.overlay_db_problems("local", bad, self.ENV)
        self.assertEqual(problems, ["ConnectionStrings:Connection does not point at the allowed test DB (host/name mismatch)"])
        self.assertNotIn("SECRET", " ".join(problems))

    def test_only_local_may_carry_connection_strings(self):
        for profile in ("cloud_staging", "cloud_preprod"):
            self.assertEqual(len(functional.overlay_db_problems(profile, {"ConnectionStrings": {"Connection": "x"}}, {})), 1)
        self.assertEqual(functional.overlay_db_problems("cloud_preprod", {"AppSettings": {"Url": "x"}}, {}), [])

    def test_overlay_connection_strings_replace_the_local_db_connection_key(self):
        env = {"local.db.allowed_host": "h", "local.db.allowed_name": "n"}
        self.assertIn("missing local.db.connection", functional.env_problems("local", env))
        self.assertEqual(functional.env_problems("local", env, db_in_overlay=True), [])

    def test_url_problems_name_the_key_not_the_value(self):
        self.assertEqual(functional.url_problems("cloud_preprod", {"A": "https://x.net/p", "B": "ftp://h", "C": "nonsense"}),
                         ["cloud_preprod.B is not an http(s) URL", "cloud_preprod.C is not an http(s) URL"])

    def test_load_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = functional.overlay_path(Path(tmp), "local")
            self.assertEqual(path.name, "appsettings.local.json")
            self.assertIsNone(functional.load_overlay(path))
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(ValueError):
                functional.load_overlay(path)
            path.write_text('{"A": 1}', encoding="utf-8")
            self.assertEqual(functional.load_overlay(path), {"A": 1})

    def test_gate_refuses_while_files_are_patched(self):
        state = {"functional_mode": "skip", "functional_skip": {"reason": "x"}, "env_patched": {"a.ts": {}}}
        self.assertEqual(functional.gate_problem(state, lambda: "f")[0], "ENV_PATCHED")


class GateTests(unittest.TestCase):
    def gate(self, state, fp="abc"):
        return functional.gate_problem(state, lambda: fp)

    def test_unset_or_later_is_refused(self):
        self.assertEqual(self.gate({})[0], "FUNCTIONAL_MODE_REQUIRED")
        self.assertEqual(self.gate({"functional_mode": "later"})[0], "FUNCTIONAL_MODE_REQUIRED")

    def test_run_needs_a_passing_current_record(self):
        run = {"functional_mode": "run"}
        self.assertEqual(self.gate(run)[0], "FUNCTIONAL_REQUIRED")
        self.assertEqual(self.gate({**run, "functional": {"status": "FAIL", "fingerprint": "abc"}})[0],
                         "FUNCTIONAL_REQUIRED")
        self.assertEqual(self.gate({**run, "functional": {"status": "PASS", "fingerprint": "old"}})[0],
                         "FUNCTIONAL_STALE")
        self.assertIsNone(self.gate({**run, "functional": {"status": "PASS", "fingerprint": "abc"}}))

    def test_skip_needs_a_recorded_reason(self):
        self.assertEqual(self.gate({"functional_mode": "skip"})[0], "FUNCTIONAL_MODE_REQUIRED")
        self.assertIsNone(self.gate({"functional_mode": "skip", "functional_skip": {"reason": "no screen"}}))


if __name__ == "__main__":
    unittest.main()
